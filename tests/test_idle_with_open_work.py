"""Idle-with-open-work diagnostic: recorded-at-finish observation, never live-joined."""
from unittest.mock import patch

from support import Fixture, ScriptDriver
from mizu.errors import Denied
from mizu.fs import now
from mizu.project import IDLE_NO_PROGRESS_OUTCOMES, idle_with_open_work
from mizu.runtime import Engine, _progress_record
from mizu.snapshot import PROGRESS_SIGNALS, progress_projection, validate_progress


class IdleWithOpenWorkTests(Fixture):
    def item(self, key, status="open", wait=None):
        item = self.project.work_items.create(
            idempotency_key=key, title=f"Idle candidate {key}", actor="worker")["record"]["work_item"]
        if status != "open":
            self.project.work_items.transition(item["id"], expected_rev=1, actor="worker",
                                               status=status, wait=wait)
        return item

    def park(self, outcome="continue", has_progress=False, ready_work="legacy"):
        """Directly persist a snapshot; ready_work True/False/None -> schema 2, 'legacy' -> schema 1."""
        current = self.project.snapshots.get()
        selected = list(self.config.limits.progress_signals)
        if has_progress:
            signals = {name: False for name in PROGRESS_SIGNALS}
            signals[selected[0]] = True
        else:
            signals = {name: False for name in PROGRESS_SIGNALS}
        if ready_work != "legacy":
            # Build schema-2 record via _progress_record for validity.
            tmp = _progress_record(current, "worker", tuple(selected), signals, outcome,
                                   obligations_digest="0" * 64, ready_work=ready_work)
            progress = tmp
        else:
            progress = {"schema": 1, "signals": signals,
                        "selected": selected, "role": "worker",
                        "has_progress": bool(has_progress),
                        "no_progress_count": 0 if has_progress else 1,
                        "obligations_digest": "0" * 64, "recorded_at": now()}
        snapshot = self.project.snapshots.create(
            self.project.snapshots.capture_files(self.project.workspace),
            goal=self.project.goal, state="Parked", run=None, outcome=outcome,
            summary="Parked test", wake_at=4102444800 if outcome == "wait" else None,
            inbox_seen=self.project.insights.generation(), wake_generation="test-wake",
            progress=progress, parent=current["id"])
        self.project.snapshots.publish(snapshot)
        return self.project.snapshots.get()

    def status_idle(self):
        return self.project.status()["idle_with_open_work"]

    def ensure_baseline(self, state="Parked"):
        """Sync inbox/wake into a parked snapshot so the next writer run is input-quiet."""
        snapshot = self.project.snapshots.get()
        generation = self.project.insights.generation()
        wake = self.project.control().get("wake_generation", "")
        if snapshot.get("inbox_seen", "") == generation and snapshot.get("wake_generation", "") == wake:
            return snapshot
        selected = list(self.config.limits.progress_signals)
        signals = {name: False for name in PROGRESS_SIGNALS}
        progress = {"schema": 1, "signals": signals,
                    "selected": selected, "role": "worker",
                    "has_progress": False, "no_progress_count": 1,
                    "obligations_digest": "0" * 64, "recorded_at": now()}
        created = self.project.snapshots.create(
            self.project.snapshots.capture_files(self.project.workspace),
            goal=self.project.goal, state=state, run=None, outcome="continue",
            summary="Baseline sync", inbox_seen=generation,
            wake_generation=wake, progress=progress, parent=snapshot["id"])
        self.project.snapshots.publish(created)
        return self.project.snapshots.get()

    def run_writer(self, outcome="wait", with_progress=False, state="Parked"):
        self.ensure_baseline(state=state)
        def finish(ctx, prompt, profile):
            if with_progress:
                ctx.progress_tool = True
            ctx.handle("finish", {"outcome": outcome, "summary": "Writer finished",
                                  "state": state})
        Engine(self.config, driver=ScriptDriver(finish)).run(self.project, "worker")
        return self.project.snapshots.get()

    # --- recorded diagnostic basics ---

    def test_only_wait_and_continue_can_mark_idle(self):
        self.assertEqual(tuple(IDLE_NO_PROGRESS_OUTCOMES), ("wait", "continue"))
        for outcome in ("wait", "continue"):
            snap = self.park(outcome, has_progress=False, ready_work=True)
            idle = idle_with_open_work(snap)
            self.assertTrue(idle["active"], outcome)
            self.assertEqual(idle["reason"], "no_progress_with_ready_work")
            self.assertIs(idle["ready_work"], True)
            self.assertEqual(idle["outcome"], outcome)
            # No work-item descriptions leave the record: only existence.
            self.assertNotIn("items", idle)
            self.assertNotIn("Idle candidate", repr(idle))

    def test_bounded_never_marks_idle_even_with_recorded_ready_work(self):
        snap = self.park("bounded", has_progress=False, ready_work=True)
        idle = idle_with_open_work(snap)
        self.assertFalse(idle["active"])
        self.assertEqual(idle["reason"], "outcome_not_applicable")
        self.assertIs(idle["ready_work"], True)

    def test_done_never_marks_idle(self):
        snap = self.park("done", has_progress=False, ready_work=True)
        idle = idle_with_open_work(snap)
        self.assertFalse(idle["active"])
        self.assertEqual(idle["reason"], "outcome_not_applicable")

    def test_progress_clears_recorded_ready_work(self):
        snap = self.park("continue", has_progress=True, ready_work=True)
        idle = idle_with_open_work(snap)
        self.assertFalse(idle["active"])
        self.assertEqual(idle["reason"], "has_progress")
        self.assertIs(idle["has_progress"], True)
        self.assertIs(idle["ready_work"], True)

    def test_recorded_false_and_empty_queue_stay_inactive(self):
        snap = self.park("wait", has_progress=False, ready_work=False)
        idle = idle_with_open_work(snap)
        self.assertFalse(idle["active"])
        self.assertEqual(idle["reason"], "no_ready_work")
        self.assertIs(idle["ready_work"], False)

    def test_recorded_unknown_fails_closed(self):
        snap = self.park("wait", has_progress=False, ready_work=None)
        idle = idle_with_open_work(snap)
        self.assertFalse(idle["active"])
        self.assertEqual(idle["reason"], "unreadable_work_state")
        self.assertIsNone(idle["ready_work"])

    def test_legacy_schema1_and_missing_progress_stay_inactive(self):
        snap = self.park("continue", has_progress=False, ready_work="legacy")
        self.assertEqual(snap["progress"]["schema"], 1)
        self.assertNotIn("ready_work", snap["progress"])
        idle = idle_with_open_work(snap)
        self.assertFalse(idle["active"])
        self.assertEqual(idle["reason"], "legacy_no_progress_record")
        legacy = {k: v for k, v in snap.items() if k != "progress"}
        idle = idle_with_open_work(legacy)
        self.assertFalse(idle["active"])
        self.assertEqual(idle["reason"], "legacy_no_progress_record")
        malformed = dict(snap)
        malformed["progress"] = {"schema": 1}
        idle = idle_with_open_work(malformed)
        self.assertFalse(idle["active"])
        self.assertEqual(idle["reason"], "invalid_progress_record")
        # Invalid ready_work type is rejected at validation.
        bad = dict(snap["progress"])
        bad["ready_work"] = "yes"
        bad["schema"] = 2
        with self.assertRaises(Denied):
            validate_progress(bad)
        bad2 = dict(snap["progress"])
        bad2["schema"] = 2
        with self.assertRaises(Denied):
            validate_progress(bad2)  # schema 2 without the field
        bad3 = dict(snap["progress"])
        bad3["schema"] = 3
        with self.assertRaises(Denied):
            validate_progress(bad3)

    def test_projection_preserves_schema_and_ready_work(self):
        snap_true = self.park("wait", has_progress=False, ready_work=True)
        projected = progress_projection(snap_true["progress"])
        self.assertEqual(projected["schema"], 2)
        self.assertIs(projected["ready_work"], True)
        snap_legacy = self.park("wait", has_progress=False, ready_work="legacy")
        projected_legacy = progress_projection(snap_legacy["progress"])
        self.assertEqual(projected_legacy["schema"], 1)
        self.assertNotIn("ready_work", projected_legacy)
        # Status projection carries the same.
        record = self.project.status()["progress"]["record"]
        self.assertEqual(record["schema"], 1)  # last published is legacy
        self.park("wait", has_progress=False, ready_work=False)
        record2 = self.project.status()["progress"]["record"]
        self.assertEqual(record2["schema"], 2)
        self.assertIs(record2["ready_work"], False)

    # --- recorded at finish: Engine capture ---

    def test_writer_run_binds_true_and_single_row_probe(self):
        self.item("ready-at-finish")
        seen = {}
        real = self.project.work_items.page_ready

        def probe(*, limit=25):
            seen["limit"] = limit
            return real(limit=limit)

        with patch.object(self.project.work_items, "page_ready", side_effect=probe):
            snap = self.run_writer("wait")
        self.assertEqual(seen.get("limit"), 1)
        self.assertEqual(snap["progress"]["schema"], 2)
        self.assertIs(snap["progress"]["ready_work"], True)
        idle = self.status_idle()
        self.assertTrue(idle["active"])
        self.assertEqual(idle["snapshot"], snap["id"])

    def test_writer_run_binds_false_when_queue_empty(self):
        snap = self.run_writer("continue")
        self.assertIs(snap["progress"]["ready_work"], False)
        idle = self.status_idle()
        self.assertFalse(idle["active"])
        self.assertEqual(idle["reason"], "no_ready_work")

    def test_writer_run_binds_unknown_when_probe_unreadable(self):
        self.item("ready-but-unreadable")
        real = self.project.work_items.page_ready

        def unreadable_finish_probe(*, limit=25):
            if limit == 1:
                raise Denied("unsafe")
            return real(limit=limit)

        with patch.object(self.project.work_items, "page_ready", side_effect=unreadable_finish_probe):
            snap = self.run_writer("wait")
        self.assertEqual(snap["progress"]["schema"], 2)
        self.assertIs(snap["progress"]["ready_work"], None)
        idle = self.status_idle()
        self.assertFalse(idle["active"])
        self.assertEqual(idle["reason"], "unreadable_work_state")

    def test_status_performs_no_live_probe(self):
        snap = self.run_writer("wait")
        self.assertIs(snap["progress"]["ready_work"], False)
        self.item("later-work")
        # Even though live work is now ready, the recorded False stands.
        with patch.object(self.project.work_items, "page_ready",
                           side_effect=AssertionError("status must not probe")):
            idle = self.status_idle()
        self.assertFalse(idle["active"])
        self.assertEqual(idle["reason"], "no_ready_work")
        self.assertEqual(idle["snapshot"], snap["id"])

    def test_recorded_true_survives_drop_and_due_transitions(self):
        item = self.item("ready-then-dropped")
        snap = self.run_writer("continue")
        self.assertTrue(self.status_idle()["active"])
        # Drop the item: the old run's diagnostic must not change.
        self.project.work_items.transition(item["id"], expected_rev=1, actor="worker",
                                           status="dropped", reason="Settled for next outcome")
        idle = self.status_idle()
        self.assertTrue(idle["active"])
        self.assertEqual(idle["snapshot"], snap["id"])
        self.assertIs(idle["ready_work"], True)

    def test_recorded_false_survives_later_creation_and_due_wait(self):
        snap = self.run_writer("wait")
        self.assertEqual(self.status_idle()["reason"], "no_ready_work")
        # Later creation cannot retroactively mark the old run idle.
        self.item("created-after-finish")
        self.assertEqual(self.status_idle()["reason"], "no_ready_work")
        self.assertEqual(self.status_idle()["snapshot"], snap["id"])
        self.assertFalse(self.status_idle()["active"])
        # Nor can a blocked wait becoming satisfied.
        self.item("blocked-after", "blocked", {"kind": "deadline", "at": "2000-01-01T00:00:00+00:00"})
        self.assertEqual(self.status_idle()["reason"], "no_ready_work")
        self.assertFalse(self.status_idle()["active"])

    def test_blocked_due_at_finish_records_ready(self):
        self.item("blocked-due", "blocked", {"kind": "deadline", "at": "2000-01-01T00:00:00+00:00"})
        snap = self.run_writer("continue")
        self.assertIs(snap["progress"]["ready_work"], True)
        self.assertTrue(self.status_idle()["active"])

    def test_blocked_without_due_at_finish_records_not_ready(self):
        self.item("blocked-plain", "blocked")
        snap = self.run_writer("wait")
        self.assertIs(snap["progress"]["ready_work"], False)
        self.assertEqual(self.status_idle()["reason"], "no_ready_work")

    def test_doing_counts_as_ready_at_finish(self):
        item = self.item("doing-item")
        self.project.work_items.transition(item["id"], expected_rev=1, status="doing", actor="worker")
        snap = self.run_writer("continue")
        self.assertIs(snap["progress"]["ready_work"], True)
        self.assertTrue(self.status_idle()["active"])

    def test_progress_run_records_but_clears_diagnostic(self):
        self.item("ready-with-progress")
        snap = self.run_writer("continue", with_progress=True)
        self.assertTrue(snap["progress"]["has_progress"])
        self.assertIs(snap["progress"]["ready_work"], True)
        idle = self.status_idle()
        self.assertFalse(idle["active"])
        self.assertEqual(idle["reason"], "has_progress")

    def test_read_only_run_does_not_move_recorded_diagnostic(self):
        self.item("writer-work")
        snap = self.run_writer("continue")
        before = self.status_idle()
        self.assertTrue(before["active"])
        engine = Engine(self.config, driver=ScriptDriver())
        engine.run(self.project, "reviewer")
        after = self.status_idle()
        self.assertEqual(after, before)
        self.assertEqual(after["snapshot"], snap["id"])
        # Read-only run created no snapshot.
        self.assertEqual(self.project.snapshots.get()["id"], snap["id"])

    def test_unreadable_at_finish_stays_unknown_after_later_changes(self):
        self.item("ready-unreadable")
        real = self.project.work_items.page_ready

        def unreadable_finish_probe(*, limit=25):
            if limit == 1:
                raise OSError("gone")
            return real(limit=limit)

        with patch.object(self.project.work_items, "page_ready", side_effect=unreadable_finish_probe):
            snap = self.run_writer("wait")
        self.assertIs(snap["progress"]["ready_work"], None)
        self.assertEqual(self.status_idle()["reason"], "unreadable_work_state")
        # Later ledger changes do not resolve the recorded unknown.
        self.item("later-work")
        idle = self.status_idle()
        self.assertEqual(idle["reason"], "unreadable_work_state")
        self.assertEqual(idle["snapshot"], snap["id"])
