"""Internal work-item seeds, append-only CAS, evidence and fail-closed reads."""
import concurrent.futures
import contextlib
import copy
import dataclasses
import datetime as dt
import json
import os
import threading
import unittest
from unittest.mock import Mock, patch

from support import Fixture, forget_ledger, history_write_json as write_json
from mizu import acceptance, work_items as work_items_module
from mizu.config import MAX_HISTORY_TEMPORARIES
from mizu.errors import Denied, LimitExceeded, SnapshotConflict
from mizu.fs import PREVIEW_BYTES, canonical
from mizu.work_items import (
    MAX_HISTORY_PAGE_BYTES, MAX_OPEN_CURSOR_CHARS, MAX_OPEN_ROW_BYTES,
    MAX_PROJECTION_BYTES, MAX_REVISIONS,
    WorkItems, _entry_stat, _timestamp_order,
)


class WorkItemTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = self.project.work_items
        self.seed = self.store.create(idempotency_key="bounded", title="Bounded lifecycle",
                                      actor="worker")["record"]["work_item"]
        self.item_id = self.seed["id"]
        self.seed_path = self.project.root / "work-items" / self.item_id / "create.json"
        self.directory = self.project.root / "work-items" / self.item_id
        self.serial = 0

    def revision_names(self):
        """Revision files beside the immutable creation record."""
        return sorted(p.name for p in self.directory.iterdir() if p.name != "create.json")

    def change(self, status="doing", expected_rev=1, **kwargs):
        return self.store.transition(self.item_id, expected_rev=expected_rev, status=status,
                                     actor=kwargs.pop("actor", "worker"), **kwargs)

    def bind(self, snapshot, expected_rev=2):
        return self.change("doing", expected_rev=expected_rev,
                           snapshot_id=snapshot["id"])

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

    def status_readers(self):
        return (self.store.count_open, self.store.count_open_readonly,
                self.store.page_open, self.store.page_open_readonly, self.store.page_ready,
                self.store.blocked_liveness_readonly, self.store.page_open_for_order)

    def test_nonterminal_revision_bumps_watched_directory_mtime(self):
        watched = self.project.root / "work-items"
        old = watched.stat().st_mtime_ns - 1_000_000_000
        os.utime(watched, ns=(old, old))
        self.assertEqual(self.change("doing")["status"], "doing")
        self.assertGreater(watched.stat().st_mtime_ns, old)

    def test_readonly_page_detects_changed_revision_file(self):
        self.change("doing")
        self.store.page_open_readonly(limit=100)
        (self.directory / "2.json").write_text("{")
        forget_ledger(self.project.root)
        with self.assertRaises(Denied):
            self.store.page_open_readonly(limit=100)

    def test_readonly_page_invalidates_projection_on_new_revision(self):
        self.store.page_open_readonly(limit=100)
        changed = self.change("doing")
        page = self.store.page_open_readonly(limit=100)
        row = next(row for row in page["items"] if row["id"] == self.item_id)
        self.assertEqual((row["status"], row["rev"]), ("doing", changed["rev"]))

    def test_status_projections_skip_superseded_snapshots_and_validate_exact_sources(self):
        self.change("doing")
        historical = self.snapshot("valid")
        self.bind(historical)
        current_snapshot = self.snapshot("valid")
        self.bind(current_snapshot, expected_rev=3)
        blocked = self.change("blocked", expected_rev=4,
                              wait={"kind": "deadline", "at": "2000-01-01T00:00:00+00:00"})
        readers = self.status_readers()
        with patch("mizu.work_item_waits.now", return_value="2030-01-01T00:00:00+00:00"):
            expected = [reader() for reader in readers]
        self.assertEqual(expected[:2], [1, 1])
        for page in (*expected[2:6], expected[6][0]):
            self.assertEqual(page["total"], 1)
            row = page["items"][0]
            self.assertEqual((row["id"], row["rev"]), (self.item_id, blocked["rev"]))
            self.assertTrue({"delivery", "snapshot_id", "verification"}.isdisjoint(row))
        self.assertEqual(expected[4]["items"][0]["wait_liveness"]["state"], "due")
        self.assertEqual(expected[5]["items"][0]["state"], "due")
        # Status readers come from the index and read no snapshot manifest.
        with patch("mizu.work_item_waits.now", return_value="2030-01-01T00:00:00+00:00"), \
                patch.object(self.store.snapshots, "binding", side_effect=AssertionError("manifest read")):
            for reader, result in zip(readers, expected):
                with self.subTest(reader=reader.__name__):
                    self.assertEqual(reader(), result)
        # A rebuild validates the current delivery against its exact manifest.
        forget_ledger(self.project.root)
        fresh = WorkItems(self.project.root, self.project.snapshots, self.project.ledger)
        with patch.object(fresh.snapshots, "binding", side_effect=Denied("snapshot unavailable")) as get:
            with self.assertRaisesRegex(Denied, "snapshot unavailable"):
                fresh.page_open_readonly(limit=100)
            get.assert_called_once_with(current_snapshot["id"])

        with patch.object(self.store.snapshots, "binding", side_effect=Denied("snapshot unavailable")) as get:
            with self.assertRaisesRegex(Denied, "snapshot unavailable"):
                self.change("done", expected_rev=5, snapshot_id=current_snapshot["id"])
            get.assert_called_once_with(historical["id"])
        self.assertEqual(self.store.get(self.item_id), blocked)
        self.assertFalse((self.directory / "6.json").exists())

    def test_status_projections_still_validate_delivery_and_transition_fields(self):
        self.change("doing")
        self.bind(self.snapshot("valid"))
        path = self.directory / "3.json"
        binding = json.loads(path.read_text())
        corruptions = [dict(binding, title="Rewritten seed"),
                       dict(binding, transition={"from": "open", "to": "doing"}),
                       dict(binding, unexpected=True)]
        for field, value in (("code_digest", "not-a-digest"), ("code_digest", None),
                             ("code_digest", "0" * 64), ("snapshot_id", "invalid"),
                             ("binding_rev", 2), ("scope_digest", "0" * 64)):
            corruptions.append({**binding, "delivery": {**binding["delivery"], field: value}})
        for index, corrupt in enumerate(corruptions):
            write_json(path, corrupt)
            forget_ledger(self.project.root)
            for reader in self.status_readers():
                with self.subTest(corruption=index, reader=reader.__name__):
                    if reader == self.store.blocked_liveness_readonly:
                        result = reader()
                        self.assertEqual(result["diagnostic"], "unreadable")
                        self.assertIsNone(result["total"])
                        self.assertFalse(result["coverage_complete"])
                    else:
                        with self.assertRaises(Denied):
                            reader()
        mismatch = {**binding, "delivery": {**binding["delivery"], "code_digest": "0" * 64}}
        write_json(path, mismatch)
        forget_ledger(self.project.root)
        with self.assertRaisesRegex(Denied, "Delivery does not match its immutable snapshot"):
            self.store.count_open_readonly()
        write_json(path, binding)
        # The newest transition is checked too, not just the binding revision.
        write_json(self.directory / "4.json", {**binding, "rev": 4})
        forget_ledger(self.project.root)
        with self.assertRaises(Denied):
            self.store.count_open_readonly()

    def test_entry_stat_uses_full_windows_link_metadata(self):
        entry = Mock(path=r"C:\store\entry", stat=Mock())
        expected = object()
        with patch("mizu.work_items.IS_WINDOWS", True), \
                patch("mizu.work_items.os.stat", return_value=expected) as path_stat:
            self.assertIs(_entry_stat(entry), expected)
        path_stat.assert_called_once_with(entry.path, follow_symlinks=False)
        entry.stat.assert_not_called()

    def test_explain_pages_seed_and_terminal_done_evidence(self):
        self.change("doing", reason="Started")
        snapshot = self.snapshot("valid")
        self.bind(snapshot)
        self.change("done", expected_rev=3, reason="Verified", snapshot_id=snapshot["id"])

        first = self.store.explain(self.item_id, limit=2)
        self.assertEqual(first["item"]["status"], "done")
        self.assertEqual(first["item"]["rev"], 4)
        self.assertEqual(first["history_total"], 4)
        self.assertEqual(first["cursor"], 0)
        self.assertEqual([record["rev"] for record in first["history"]], [1, 2])
        self.assertEqual(first["next_cursor"], 2)
        self.assertTrue(first["truncated"])
        second = self.store.explain(self.item_id, after_rev=first["next_cursor"], limit=2)
        self.assertEqual([record["rev"] for record in second["history"]], [3, 4])
        self.assertIsNone(second["next_cursor"])
        self.assertFalse(second["truncated"])
        self.assertEqual(second["history"][1]["reason"], "Verified")
        self.assertEqual(second["history"][1]["snapshot_id"], snapshot["id"])
        self.assertEqual(second["history"][1]["verification"], snapshot["verification"])

    def test_explain_validates_current_and_selected_snapshot_evidence_only(self):
        self.change("doing")
        snapshots = []
        for index in range(3):
            snapshot = self.snapshot("valid")
            self.bind(snapshot, expected_rev=2 + index)
            snapshots.append(snapshot)
        current = self.store.get(self.item_id)
        while current["rev"] < 17:
            current = self.change("open" if current["status"] == "doing" else "doing",
                                  expected_rev=current["rev"])

        with patch.object(self.store.snapshots, "binding", wraps=self.store.snapshots.binding) as get, \
                patch.object(self.store, "_validate_transition", wraps=self.store._validate_transition) as validate:
            first = self.store.explain(self.item_id, after_rev=0, limit=1)
            self.assertEqual([row["rev"] for row in first["history"]], [1])
            get.assert_called_once_with(snapshots[2]["id"])
            self.assertEqual(validate.call_count, 16)

        with patch.object(self.store.snapshots, "binding", wraps=self.store.snapshots.binding) as get:
            selected = self.store.explain(self.item_id, after_rev=2, limit=2)
            self.assertEqual([row["rev"] for row in selected["history"]], [3, 4])
            self.assertEqual(get.call_count, 3)
            self.assertEqual({call.args[0] for call in get.call_args_list},
                             {snapshot["id"] for snapshot in snapshots})

        with patch.object(self.store.snapshots, "binding", wraps=self.store.snapshots.binding) as get:
            repeated = self.store.explain(self.item_id, after_rev=4, limit=3)
            self.assertEqual([row["rev"] for row in repeated["history"]], [5, 6, 7])
            get.assert_called_once_with(snapshots[2]["id"])

        # A corrupt snapshot outside the returned history page is skipped;
        # selecting its delivery makes the same file fail closed.
        old_snapshot_path = (self.project.root / "snapshots"
                             / f"{snapshots[0]['id']}.json")
        old_snapshot = json.loads(old_snapshot_path.read_text())
        corrupt_snapshot = dict(old_snapshot, summary="tampered")
        write_json(old_snapshot_path, corrupt_snapshot)
        with patch.object(self.store.snapshots, "binding", wraps=self.store.snapshots.binding) as get:
            page = self.store.explain(self.item_id, after_rev=3, limit=1)
            self.assertEqual([row["rev"] for row in page["history"]], [4])
            self.assertNotIn(snapshots[0]["id"], {call.args[0] for call in get.call_args_list})
        with self.assertRaises(Denied):
            self.store.explain(self.item_id, after_rev=2, limit=1)
        write_json(old_snapshot_path, old_snapshot)

        # A valid-format mismatch in an unselected historical binding does not
        # poison a page that returns later evidence, but is rejected if selected.
        old_path = self.directory / "3.json"
        old = json.loads(old_path.read_text())
        old["delivery"]["code_digest"] = "0" * 64
        write_json(old_path, old)
        with patch.object(self.store.snapshots, "binding", wraps=self.store.snapshots.binding) as get:
            page = self.store.explain(self.item_id, after_rev=3, limit=1)
            self.assertEqual([row["rev"] for row in page["history"]], [4])
            self.assertEqual({call.args[0] for call in get.call_args_list},
                             {snapshots[1]["id"], snapshots[2]["id"]})
        with self.assertRaisesRegex(Denied, "Delivery does not match its immutable snapshot"):
            self.store.explain(self.item_id, after_rev=2, limit=1)

    def test_explain_reads_dropped_terminal_item_and_preserves_reason(self):
        dropped = self.change("dropped", reason="No longer needed")
        view = self.store.explain(self.item_id)
        self.assertEqual(view["item"]["status"], "dropped")
        self.assertEqual(view["item"]["rev"], 2)
        self.assertEqual([row["rev"] for row in view["history"]], [1, 2])
        self.assertEqual(view["history"][1], dropped)
        self.assertEqual(view["history"][1]["reason"], "No longer needed")
        self.assertIsNone(view["next_cursor"])

    def test_explain_rejects_bad_id_cursor_and_limit(self):
        for item_id, after_rev, limit in (("wi_bad", 0, 25), (self.item_id, -1, 25),
                                          (self.item_id, True, 25), (self.item_id, 2, 25),
                                          (self.item_id, 257, 25), (self.item_id, 0, 0),
                                          (self.item_id, 0, 101)):
            with self.assertRaises(Denied):
                self.store.explain(item_id, after_rev=after_rev, limit=limit)

    def test_explain_byte_bound_still_replays_unreturned_chain(self):
        current = self.seed
        for index in range(10):
            target = "doing" if current["status"] != "doing" else "open"
            current = self.store.transition(
                self.item_id, expected_rev=current["rev"], status=target, actor="worker",
                reason=f"revision-{index}-" + "r" * 100000)
        page = self.store.explain(self.item_id, limit=100)
        self.assertEqual(page["item"]["rev"], 11)
        self.assertTrue(page["truncated"])
        self.assertIsNotNone(page["next_cursor"])
        self.assertEqual([row["rev"] for row in page["history"]],
                         list(range(1, page["next_cursor"] + 1)))
        self.assertLessEqual(len(canonical(page)), MAX_HISTORY_PAGE_BYTES)

        # This final revision lies beyond the retained page. A by-ID read must
        # still replay it and reject the broken chain rather than return a page.
        final_path = self.directory / "11.json"
        final_record = json.loads(final_path.read_text())
        final_record["actor"] = "invalid actor"
        write_json(final_path, final_record)
        with self.assertRaises(Denied):
            self.store.explain(self.item_id, limit=100)

    def test_projection_is_byte_bounded(self):
        event = self.change(reason="r" * (PREVIEW_BYTES - 2048))
        view = self.store.projection(self.item_id)
        self.assertEqual(view["item"], event)
        self.assertLessEqual(len(canonical(view)), MAX_PROJECTION_BYTES)

    def test_stale_and_invalid_revisions_write_nothing(self):
        self.change()
        original = (self.directory / "2.json").read_bytes()
        for revision in (1, 0, -1, True, False, 2.0, "2", None):
            with self.subTest(revision=revision), self.assertRaises(Denied):
                self.change("open", expected_rev=revision)
        self.assertEqual(self.revision_names(), ["2.json"])
        self.assertEqual((self.directory / "2.json").read_bytes(), original)

    def test_done_rejects_missing_unpublished_unverified_failed_and_mismatched_evidence(self):
        candidates = [None, "bad", "f" * 64, self.snapshot("valid", publish=False)["id"],
                      self.snapshot()["id"], self.snapshot({"passed": False, "code_digest": "a" * 64})["id"],
                      self.snapshot({"passed": True, "code_digest": "a" * 64})["id"],
                      self.snapshot({"passed": 1, "code_digest": self.project.snapshots.get()["code_digest"]})["id"]]
        for sid in candidates:
            with self.subTest(sid=sid), self.assertRaises(Denied):
                self.change("done", snapshot_id=sid)
        self.assertEqual(self.revision_names(), [])

    def test_done_binds_the_published_tree_that_carries_the_delivery(self):
        self.change("doing", reason="Start the work item")
        snapshot = self.snapshot("valid")
        presented = self.project.snapshots.capture_files(self.project.workspace)["code_digest"]
        self.bind(snapshot)
        completed = self.change("done", expected_rev=3, snapshot_id=snapshot["id"], reason="Verified")
        self.assertEqual(completed["delivered_tree"], presented)
        self.assertEqual(completed["delivered_tree"], snapshot["code_digest"])
        self.assertEqual(self.store.get(self.item_id)["delivered_tree"], presented)

    def test_done_refuses_a_snapshot_without_a_delivery_binding(self):
        self.change("doing", reason="Start the work item")
        snapshot = self.snapshot("valid")
        # Moving the workspace does not establish an item delivery binding.
        (self.project.workspace / "app.py").write_bytes(b"VALUE = 3\n")
        with self.assertRaises(Denied):
            self.change("done", expected_rev=2, snapshot_id=snapshot["id"])
        self.assertEqual(self.store.get(self.item_id)["status"], "doing")
        self.assertEqual(self.revision_names(), ["2.json"])

    def test_done_accepts_an_older_published_snapshot_carrying_the_same_tree(self):
        self.change("doing", reason="Start the work item")
        older = self.snapshot("valid")
        newer = self.snapshot("valid")
        self.assertEqual(older["code_digest"], newer["code_digest"])
        self.assertNotEqual(older["id"], newer["id"])
        self.bind(older)
        completed = self.change("done", expected_rev=3, snapshot_id=older["id"])
        self.assertEqual(completed["delivered_tree"], older["code_digest"])

    def test_done_refuses_a_closure_without_a_delivery_binding(self):
        store = WorkItems(self.project.root, self.project.snapshots, self.project.ledger)
        snapshot = self.snapshot("valid")
        with self.assertRaises(Denied):
            store.transition(self.item_id, expected_rev=1, status="done", actor="worker",
                             snapshot_id=snapshot["id"])
        self.assertEqual(self.revision_names(), [])

    def test_done_record_must_name_the_tree_its_snapshot_carries(self):
        snapshot = self.snapshot("valid")
        self.change()
        self.bind(snapshot)
        completed = self.change("done", expected_rev=3, snapshot_id=snapshot["id"])
        path = self.directory / "4.json"
        tampered = copy.deepcopy(completed)
        tampered["delivered_tree"] = "a" * 64
        write_json(path, tampered)
        with self.assertRaises(Denied):
            self.store.get(self.item_id)
        # A record written before the claim existed stays readable history.
        legacy = copy.deepcopy(completed)
        for key in ("delivered_tree", "delivery_schema", "delivery", "verification_scope",
                    "completion_policy", "completion_evidence"):
            del legacy[key]
        legacy["rev"] = 2
        legacy["transition"]["from"] = "open"
        (self.directory / "3.json").unlink()
        path.unlink()
        write_json(self.directory / "2.json", legacy)
        self.assertIsNone(self.store.get(self.item_id).get("delivered_tree"))
        row = self.store.page_closed_readonly(limit=10)["items"][0]
        self.assertIsNone(row["delivered_tree"])

    def test_done_requires_retained_history_not_just_an_existing_manifest(self):
        old = self.snapshot("valid")
        self.project.snapshots.history_index = 1
        self.snapshot("valid")
        self.assertEqual(self.project.snapshots.get(old["id"]), old)
        with self.assertRaises(Denied):
            self.change("done", snapshot_id=old["id"])
        self.assertEqual(self.revision_names(), [])

    def test_sibling_snapshot_conflict_preserves_winner_and_done_evidence(self):
        snapshots = self.project.snapshots
        base = snapshots.get()
        self.change("doing", reason="Start the work item")
        captured = snapshots.capture_files(self.project.workspace)

        def candidate(goal, state, outcome, summary, run):
            verification = {"passed": True, "code_digest": captured["code_digest"], "run": run}
            return snapshots.create(
                captured, goal=goal, state=state, run=run, outcome=outcome,
                summary=summary, verification=verification, parent=base["id"])

        winner = candidate("Winning goal", "Winning state", "continue", "Winning summary", "winner-run")
        loser = candidate("Losing goal", "Losing state", "wait", "Losing summary", "loser-run")
        self.assertEqual(winner["parent"], base["id"])
        self.assertEqual(loser["parent"], base["id"])
        self.assertNotEqual(winner["id"], loser["id"])
        self.assertEqual(winner["verification"], {
            "passed": True, "code_digest": winner["code_digest"], "run": "winner-run"})
        self.assertEqual(loser["verification"], {
            "passed": True, "code_digest": loser["code_digest"], "run": "loser-run"})
        self.assertNotEqual(winner["verification"], loser["verification"])

        snapshots.publish(winner)
        winner_history = snapshots._history_ids()
        self.assertEqual(snapshots.get(), winner)
        with self.assertRaises(SnapshotConflict):
            snapshots.publish(loser)
        current = snapshots.get()
        self.assertEqual(current, winner)
        self.assertEqual(snapshots._history_ids(), winner_history)
        self.assertEqual(
            {key: current[key] for key in ("goal", "state", "outcome", "summary", "verification")},
            {key: winner[key] for key in ("goal", "state", "outcome", "summary", "verification")})

        with self.assertRaises(Denied):
            self.change("done", expected_rev=2, snapshot_id=loser["id"])
        self.assertEqual(self.store.get(self.item_id)["status"], "doing")
        self.bind(winner)
        completed = self.change("done", expected_rev=3, snapshot_id=winner["id"])
        self.assertEqual(completed["snapshot_id"], winner["id"])
        self.assertEqual(completed["verification"], winner["verification"])
        self.assertNotEqual(completed["verification"], loser["verification"])

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
        self.bind(bad)
        completed = self.change("done", expected_rev=3, snapshot_id=bad["id"], reason="Verified fix")
        self.assertIsNone(completed["holder"])
        self.assertEqual(completed["snapshot_id"], bad["id"])
        self.assertEqual(completed["verification"], bad["verification"])
        self.project.snapshots.history_index = 1
        self.snapshot()
        self.assertEqual(self.store.get(self.item_id), completed)
        for status in ("open", "doing", "blocked", "done", "dropped"):
            with self.subTest(status=status), self.assertRaises(Denied):
                self.change(status, expected_rev=4, reason="Reopen")
        self.assertEqual(self.revision_names(), ["2.json", "3.json", "4.json"])

    def test_reconstruction_rejects_completion_copy_mismatched_to_snapshot(self):
        snapshot = self.snapshot("valid")
        self.change()
        self.bind(snapshot)
        completed = self.change("done", expected_rev=3, snapshot_id=snapshot["id"])
        path = self.directory / "4.json"
        tampered = copy.deepcopy(completed)
        tampered["verification"]["code_digest"] = "a" * 64
        write_json(path, tampered)
        with self.assertRaises(Denied):
            self.store.get(self.item_id)
        # The actual manifest is also revalidated on reconstruction.
        write_json(path, completed)
        manifest = self.project.snapshots.root / "snapshots" / f"{snapshot['id']}.json"
        saved = json.loads(manifest.read_bytes())
        for evidence in ({**saved, "summary": "tampered"}, None):
            if evidence is None:
                manifest.unlink()
            else:
                write_json(manifest, evidence)
            for reader in (lambda: self.store.get(self.item_id),
                           lambda: self.store.explain(self.item_id, limit=1)):
                with self.subTest(evidence=evidence is not None, reader=reader), self.assertRaises(Denied):
                    reader()
        write_json(manifest, saved)
        self.assertEqual(self.store.get(self.item_id), completed)

    def test_drop_requires_nonempty_reason_and_is_terminal(self):
        for reason in (None, "", " \n", 1):
            with self.subTest(reason=reason), self.assertRaises(Denied):
                self.change("dropped", reason=reason)
        self.assertEqual(self.revision_names(), [])
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
        self.assertEqual(self.revision_names(), [])

    def test_oversized_transition_writes_nothing(self):
        with self.assertRaises(LimitExceeded):
            self.change(reason="x" * PREVIEW_BYTES)
        self.assertEqual(self.revision_names(), [])

    def test_status_shares_one_open_scan_for_open_count_and_blocked_work(self):
        calls = []
        original = self.store._page_open_impl

        def scanned(**kwargs):
            calls.append(kwargs)
            return original(**kwargs)

        with patch.object(self.store, "_page_open_impl", side_effect=scanned):
            status = self.project.status()
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["blocked_only"])
        self.assertTrue(calls[0]["include_open_count"])
        self.assertEqual(status["blocked_work"]["total"], 0)

    def test_history_gap_bad_names_malformed_and_oversized_events_fail_closed(self):
        event = self.change()
        path = self.directory / "2.json"
        mutations = [{**event, "rev": True}, {**event, "rev": 3}, {**event, "title": "changed"},
                     {**event, "holder": None}, {**event, "transition": {}},
                     {**event, "origin": "changed origin"},
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
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.directory / ".new-leftover"
        temporary.write_bytes(b"incomplete write")
        forget_ledger(self.project.root)  # Found by a rebuild; a write to the item recovers it too.
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
        forget_ledger(self.project.root)
        self.assertEqual(self.store.get(self.item_id), committed)
        self.assertFalse(temporary.exists())
        self.assertEqual(event_path.stat().st_nlink, 1)

    def test_atomic_temporary_symlink_fails_closed(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.directory / ".new-leftover"
        target = self.project.root / "saved-temporary"
        target.write_bytes(b"outside")
        self.symlink(target, temporary)
        forget_ledger(self.project.root)
        with self.assertRaises(Denied):
            self.store.get(self.item_id)
        self.assertTrue(target.is_file())

    def test_symlink_transition_fails_closed(self):
        self.change()
        path = self.directory / "2.json"
        target = self.project.root / "saved-transition.json"
        path.rename(target)
        self.symlink(target, path)
        forget_ledger(self.project.root)
        with self.assertRaises(Denied):
            self.store.get(self.item_id)
        with self.assertRaises(Denied):
            self.change("open", expected_rev=2)

    def test_symlink_history_directory_fails_closed(self):
        target = self.project.root / "other-items"
        target.mkdir()
        self.symlink(target, self.directory.parent, target_is_directory=True)
        forget_ledger(self.project.root)
        with self.assertRaises(Denied):
            self.store.get(self.item_id)
        with self.assertRaises(Denied):
            self.change()
        self.assertEqual(list(target.iterdir()), [])

    def test_page_open_validates_cursor_and_exact_integer_limit(self):
        for cursor in ("", "wi_" + "A" * 32, "../anything", 1, True,
                       "wi1|finished|2030-01-01T00:00:00+00:00|wi_" + "f" * 32,
                       "x" * (MAX_OPEN_CURSOR_CHARS + 1)):
            with self.subTest(cursor=cursor), self.assertRaises(Denied):
                self.store.page_open(cursor=cursor)
        for limit in (0, -1, 101, True, False, 1.0, "2", None):
            with self.subTest(limit=limit), self.assertRaises(Denied):
                self.store.page_open(limit=limit)
        self.assertEqual(self.store.page_open(limit=1)["items"][0]["id"], self.item_id)
        future = f"wi1|open|{_timestamp_order('9999-12-31T23:59:59+00:00')}|wi_" + "f" * 32
        self.assertEqual(self.store.page_open(cursor=future, limit=100)["items"], [])

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

    def test_concurrent_same_revision_has_one_winner(self):
        barrier = threading.Barrier(2)

        def attempt(actor):
            store = WorkItems(self.project.root, self.project.snapshots, self.project.ledger)
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
        self.assertEqual(self.revision_names(), ["2.json"])


    def test_continued_doing_sidecar_is_removed_and_exact(self):
        with patch("mizu.work_items.now",
                   side_effect=["2031-01-01T00:00:00+00:00",
                                "2031-01-02T00:00:00+00:00"]):
            first = self.change("doing")
            second = self.change("doing", expected_rev=2, scope="continued scope",
                                 reason="scope change while held")
        self.assertEqual(first["updated_at"], "2031-01-01T00:00:00+00:00")
        self.assertEqual(second["updated_at"], "2031-01-02T00:00:00+00:00")
        sidecar = self.project.root / "work-item-state" / f"{self.item_id}.json"
        self.assertFalse(sidecar.exists())
        fresh = WorkItems(self.project.root, self.project.snapshots, self.project.ledger)
        page = fresh.page_open_readonly(limit=100)
        row = next(row for row in page["items"] if row["id"] == self.item_id)
        self.assertEqual((row["status"], row["rev"]), ("doing", 3))
        self.assertEqual(row["doing_since"], first["updated_at"])
        self.assertFalse(row["doing_stale"])

    def test_sidecar_blocked_wait_replays_exactly(self):
        self.change("doing")
        self.store.transition(self.item_id, expected_rev=2, status="blocked", actor="worker",
                              reason="Wait for evidence",
                              wait={"kind": "deadline", "at": "2030-01-01T00:00:00+00:00"})
        expected = self.store.get(self.item_id)
        fresh = WorkItems(self.project.root, self.project.snapshots, self.project.ledger)
        page = fresh.page_open_readonly(limit=100)
        row = next(row for row in page["items"] if row["id"] == self.item_id)
        self.assertEqual((row["status"], row["rev"]), ("blocked", 3))
        self.assertEqual(row["wait"], expected["wait"])
        self.assertIsNone(row["doing_since"])
        self.assertIsNone(row["doing_age_seconds"])
        self.assertFalse(row["doing_stale"])

    def test_sidecar_invalidated_by_new_transition(self):
        self.change("doing")
        first = self.store.page_open_readonly(limit=100)
        self.assertEqual(next(row for row in first["items"] if row["id"] == self.item_id)["status"], "doing")
        self.store.transition(self.item_id, expected_rev=2, status="open", actor="worker", reason="pause")
        second = self.store.page_open_readonly(limit=100)
        row = next(row for row in second["items"] if row["id"] == self.item_id)
        self.assertEqual((row["status"], row["rev"]), ("open", 3))

    def test_sidecar_absent_legacy_item_reads_exactly(self):
        self.change("doing")
        sidecar = self.project.root / "work-item-state" / f"{self.item_id}.json"
        sidecar.unlink(missing_ok=True)
        fresh = WorkItems(self.project.root, self.project.snapshots, self.project.ledger)
        page = fresh.page_open_readonly(limit=100)
        row = next(row for row in page["items"] if row["id"] == self.item_id)
        self.assertEqual((row["status"], row["rev"]), ("doing", 2))
        self.assertEqual(fresh.get(self.item_id)["status"], "doing")

if __name__ == "__main__":
    unittest.main()
