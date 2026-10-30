"""Internal continuation protocol; local fake drivers, no provider/sandbox calls."""
import contextlib
import dataclasses
import shutil
import threading
import uuid
from unittest.mock import patch

from support import Fixture, ScriptDriver
from mizu import continuations as c, snapshot as snapshot_module, storage_runs
from mizu.admission import _Queue, role_exclusion
from mizu.errors import Busy, Denied, InfraExceeded, LimitExceeded, SnapshotConflict
from mizu.fs import lock, now, publish_pointer, read_json, write_json
from mizu.project import Project
from mizu.runtime import Engine
from mizu.storage import audit


class ContinuationFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.store = self.project.snapshots
        self.engine = Engine(self.config, driver=ScriptDriver(lambda *_: self.fail("No model calls")))

    def seal(self, outcome="continue", role="worker"):
        source = uuid.uuid4().hex
        current = self.store.get()
        write_json(self.project.root / "runs" / source / "started.json", {
            "run": source, "role": role, "snapshot": current["id"],
            "started_at": "2020-01-01T00:00:00+00:00"})
        snapshot = self.store.create(self.store.capture_files(self.project.workspace),
            goal=current["goal"], state="state", run=source, outcome=outcome,
            summary="yield", parent=current["id"])
        return source, snapshot

    def prepare(self, source, snapshot, role="worker"):
        with lock(role_exclusion(self.project.root, role)), \
                lock(self.project.root / "locks/workspace.lock"):
            return c.prepare(self.store, role=role, source_run=source, snapshot=snapshot["id"])

    def committed(self):
        source, snapshot = self.seal()
        row = self.prepare(source, snapshot)
        self.store.publish(snapshot)
        return row, snapshot


class ContinuationTests(ContinuationFixture):
    def test_prepared_without_commit_cannot_reserve(self):
        source, snapshot = self.seal()
        row = self.prepare(source, snapshot)
        self.assertEqual(row["state"], "prepared")
        self.assertEqual(self.prepare(source, snapshot), row)
        with patch.object(self.engine, "_admit", side_effect=AssertionError("must not admit")):
            with c.reservation(self.engine, self.project, "worker") as reserved:
                self.assertIsNone(reserved)
        self.assertNotEqual(self.store.get()["id"], snapshot["id"])
        self.assertEqual(c.pending(self.store, "worker"), row)

    def test_crash_before_pointer_commit_leaves_only_prepared_intent(self):
        source, snapshot = self.seal()
        row = self.prepare(source, snapshot)
        old = read_json(self.store.root / "current.json")
        with patch.object(snapshot_module, "publish_pointer", side_effect=OSError("crash before commit")):
            with self.assertRaises(OSError):
                self.store.publish(snapshot)
        # An orphan immutable history generation is not commit proof.
        self.assertGreater(len(list((self.store.root / "histories").iterdir())), 1)
        self.assertEqual(read_json(self.store.root / "current.json"), old)
        restarted = Project(self.config, self.project.name)
        self.assertEqual(c.pending(restarted.snapshots, "worker"), row)
        with c.reservation(Engine(self.config), restarted, "worker") as reserved:
            self.assertIsNone(reserved)
        self.store.publish(snapshot)
        self.assertEqual(c.pending(self.store, "worker")["state"], "committed")

    def test_crash_after_pointer_commit_recovers_exact_seal(self):
        source, snapshot = self.seal()
        original = self.prepare(source, snapshot)
        def commit_then_crash(*args, **kwargs):
            publish_pointer(*args, **kwargs)
            raise OSError("publisher died before observing success")
        with patch.object(snapshot_module, "publish_pointer", side_effect=commit_then_crash):
            with self.assertRaises(OSError):
                self.store.publish(snapshot)
        restarted = Project(self.config, self.project.name)
        self.assertEqual(restarted.snapshots.get()["id"], snapshot["id"])
        with c.reservation(Engine(self.config), restarted, "worker") as reserved:
            self.assertEqual(reserved["successor"], original["successor"])
            self.assertEqual(reserved["current_snapshot"], snapshot["id"])
        self.assertEqual(c.pending(restarted.snapshots, "worker")["state"], "reserved")

    def test_later_publications_preserve_proof_beyond_history_window(self):
        original, snapshot = self.committed()
        self.store.history_index = 1
        for _ in range(3):
            _, later = self.seal(outcome="wait")
            self.store.publish(later)
        self.assertNotIn(snapshot["id"], self.store._history_ids())
        self.assertEqual(c.pending(self.store, "worker")["snapshot"], snapshot["id"])
        with c.reservation(self.engine, self.project, "worker") as reserved:
            self.assertEqual(reserved["successor"], original["successor"])
            self.assertEqual(reserved["current_snapshot"], later["id"])
        self.assertEqual(audit(self.project)["snapshots"]["live"], 2)

    def test_cas_loser_never_commits_intent(self):
        source, snapshot = self.seal()
        self.prepare(source, snapshot)
        _, winner = self.seal(outcome="wait")
        self.store.publish(winner)
        with self.assertRaises(SnapshotConflict):
            self.store.publish(snapshot)
        self.assertEqual(c.pending(self.store, "worker")["state"], "prepared")
        with c.reservation(self.engine, self.project, "worker") as reserved:
            self.assertIsNone(reserved)

    def test_non_continue_seals_cannot_prepare(self):
        for outcome in ("done", "wait", "blocked", "bounded", "cancel", "final"):
            with self.subTest(outcome=outcome):
                source, snapshot = self.seal(outcome)
                with self.assertRaises(Denied):
                    self.prepare(source, snapshot)
        self.assertIsNone(c.pending(self.store, "worker"))

    def test_source_identity_and_input_are_checked(self):
        source, snapshot = self.seal()
        with self.assertRaises(Denied):
            self.prepare(uuid.uuid4().hex, snapshot)
        write_json(self.store.root / "runs" / source / "started.json", {
            "run": source, "role": "reporter", "snapshot": snapshot["id"]})
        with self.assertRaises(Denied):
            self.prepare(source, snapshot)
        self.assertIsNone(c.pending(self.store, "worker"))

    def test_already_published_seal_cannot_be_prepared_retroactively(self):
        source, snapshot = self.seal()
        self.store.publish(snapshot)
        with self.assertRaises(Denied):
            self.prepare(source, snapshot)

    def test_duplicate_reservations_and_restart_have_one_logical_identity(self):
        original, snapshot = self.committed()
        for _ in range(3):
            restarted = Project(self.config, self.project.name)
            with c.reservation(Engine(self.config), restarted, "worker") as reserved:
                self.assertEqual(reserved["successor"], original["successor"])
                self.assertEqual(reserved["snapshot"], snapshot["id"])
        rows = c.entries(c.pointer_at(self.store.root))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "reserved")
        # Reservation alone is not a run; only Engine accepts the identity.
        self.assertFalse((self.store.root / "runs" / original["successor"]).exists())

    def test_racing_wake_cannot_hold_same_writer_or_change_identity(self):
        original, _ = self.committed()
        errors = []
        def competing_wake():
            try:
                with c.reservation(Engine(self.config), self.project, "worker"):
                    errors.append("unexpected admission")
            except Busy:
                errors.append("busy")
        with c.reservation(self.engine, self.project, "worker") as reserved:
            thread = threading.Thread(target=competing_wake)
            thread.start()
            thread.join(60)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, ["busy"])
            self.assertEqual(reserved["successor"], original["successor"])
        with c.reservation(self.engine, self.project, "worker") as reserved:
            self.assertEqual(reserved["successor"], original["successor"])

    def test_reservation_write_failure_leaves_same_committed_intent(self):
        self.committed()
        original = c.pending(self.store, "worker")
        with patch.object(c, "publish_pointer", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError), c.reservation(self.engine, self.project, "worker"):
                pass
        self.assertEqual(c.pending(self.store, "worker"), original)
        with c.reservation(self.engine, self.project, "worker") as reserved:
            self.assertEqual(reserved["successor"], original["successor"])

    def test_stop_authority_budget_and_drain_holds_preserve_intent(self):
        self.committed()
        original = c.pending(self.store, "worker")
        self.engine.stop.set()
        with self.assertRaises(Busy), c.reservation(self.engine, self.project, "worker"):
            pass
        self.engine.stop.clear()
        for control in ({"armed": False}, {"paused": True}, {"draining": True}):
            with self.subTest(control=control):
                self.project.set_control(**control)
                with self.assertRaises((Denied, Busy)), c.reservation(self.engine, self.project, "worker"):
                    pass
                self.project.set_control(armed=True, paused=False, draining=False)
        limited = dataclasses.replace(self.config, limits=dataclasses.replace(self.config.limits, daily_requests=0))
        with self.assertRaises(InfraExceeded), c.reservation(Engine(limited), self.project, "worker"):
            pass
        self.assertEqual(c.pending(self.store, "worker"), original)

    def test_authority_is_rechecked_at_admission(self):
        self.committed()
        original = c.pending(self.store, "worker")
        admit = self.engine._admit
        def revoke(*args, **kwargs):
            self.project.set_control(paused=True)
            return admit(*args, **kwargs)
        with patch.object(self.engine, "_admit", side_effect=revoke):
            with self.assertRaises(Denied), c.reservation(self.engine, self.project, "worker"):
                pass
        self.assertEqual(c.pending(self.store, "worker"), original)

    def test_fifo_and_queue_capacity_use_existing_admission(self):
        self.committed()
        original = c.pending(self.store, "worker")
        config = dataclasses.replace(self.config, limits=dataclasses.replace(
            self.config.limits, parallel_runs=1, admission_queue_capacity=1))
        engine = Engine(config)
        resources = [role_exclusion(self.project.root, "reporter")]
        with contextlib.closing(_Queue(config, resources)) as older:
            with lock(config.data / "slots/0.lock"):
                with self.assertRaises(Busy):
                    older.attempt(enqueue=True)
                with self.assertRaisesRegex(Busy, "queue is full"), \
                        c.reservation(engine, self.project, "worker", wait=True):
                    pass
            with self.assertRaisesRegex(Busy, "Older execution admission is ready"), \
                    c.reservation(engine, self.project, "worker"):
                pass
            self.assertEqual(c.pending(self.store, "worker"), original)
            with older.attempt(enqueue=True):
                with self.assertRaises(Busy), c.reservation(engine, self.project, "worker"):
                    pass
        with c.reservation(engine, self.project, "worker") as reserved:
            self.assertEqual(reserved["successor"], original["successor"])

    def test_retention_pins_source_and_any_existing_successor(self):
        original, _ = self.committed()
        for run in (original["source_run"], original["successor"]):
            directory = self.store.root / "runs" / run
            write_json(directory / "started.json", {
                "run": run, "role": "worker", "snapshot": "a" * 64,
                "started_at": "2020-01-01T00:00:00+00:00"})
            write_json(directory / "result.json", {
                "run": run, "role": "worker", "snapshot": "a" * 64,
                "finished_at": "2020-01-01T00:00:01+00:00", "status": "completed",
                "finish": {"outcome": "continue", "state": "", "summary": "old"}})
        planned = storage_runs.plan(self.project)
        self.assertTrue(planned["complete"], planned)
        self.assertEqual(planned["pinned_runs"], 2)
        self.assertEqual(planned["components"], [])
        pointer = c.pointer_at(self.store.root)
        pointer.pop("continuations")
        write_json(self.store.root / "current.json", pointer)
        unpinned = storage_runs.plan(self.project)
        self.assertTrue(unpinned["complete"], unpinned)
        self.assertEqual({r for group in unpinned["components"] for r in group},
                         {original["source_run"], original["successor"]})

    def test_malformed_registry_fails_publication_reservation_and_retention_closed(self):
        self.committed()
        _, later = self.seal()
        pointer = c.pointer_at(self.store.root)
        pointer["continuations"]["entries"][0]["state"] = "surprise"
        write_json(self.store.root / "current.json", pointer)
        with self.assertRaises(Denied):
            self.store.publish(later)
        with self.assertRaises(Denied), c.reservation(self.engine, self.project, "worker"):
            pass
        self.assertFalse(storage_runs.plan(self.project)["complete"])
        self.assertGreater(audit(self.project)["reference_records_skipped"], 0)

    def test_registry_bounds_duplicates_and_oversized_pointer(self):
        original, _ = self.committed()
        pointer = c.pointer_at(self.store.root)
        for value in (None, {"version": True, "entries": []},
                      {"version": 1, "entries": [original] * 2},
                      {"version": 1, "entries": [original] * (c.MAX_PENDING + 1)}):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(Denied):
                    c.entries({"continuations": value})
        path = self.store.root / "current.json"
        path.write_bytes(b" " * (c.POINTER_BYTES + 1))
        with self.assertRaises(LimitExceeded):
            c.pending(self.store, "worker")
        write_json(path, pointer)

    def test_normal_continue_publication_creates_no_intent(self):
        _, snapshot = self.seal()
        self.store.publish(snapshot)
        self.assertNotIn("continuations", read_json(self.store.root / "current.json"))
        with c.reservation(self.engine, self.project, "worker") as reserved:
            self.assertIsNone(reserved)
        self.assertEqual(c.entries(c.pointer_at(self.store.root)), [])

    def test_waiting_reservation_joins_fifo_tail(self):
        self.committed()
        config = dataclasses.replace(self.config, limits=dataclasses.replace(
            self.config.limits, parallel_runs=1, admission_queue_capacity=2))
        engine = Engine(config)
        attempt = _Queue.attempt
        observed = []
        with contextlib.closing(_Queue(config, [role_exclusion(self.project.root, "reporter")])) as older:
            with lock(config.data / "slots/0.lock"):
                with self.assertRaises(Busy):
                    older.attempt(enqueue=True)
            def ordered_attempt(candidate, **kwargs):
                try:
                    return attempt(candidate, **kwargs)
                except Busy:
                    if candidate is not older and not observed:
                        tickets = read_json(config.data / "slots/queue.json")["tickets"]
                        observed.append([t["id"] for t in tickets])
                        self.assertEqual(observed[0], [older.identity, candidate.identity])
                        with attempt(older, enqueue=True):
                            pass
                    raise
            # Deterministically service older demand at the first refusal;
            # no sleeps, wall-clock races, child processes, or paid calls.
            with patch.object(_Queue, "attempt", ordered_attempt), \
                    patch.object(engine.stop, "wait", return_value=False):
                with c.reservation(engine, self.project, "worker", wait=True) as reserved:
                    self.assertEqual(reserved["state"], "reserved")
        self.assertEqual(len(observed), 1)
        self.assertEqual(read_json(config.data / "slots/queue.json")["tickets"], [])

    def test_authority_revoked_after_slot_grant_does_not_reserve(self):
        self.committed()
        original = c.pending(self.store, "worker")
        check = self.engine._check_admission
        calls = []
        def recheck(project):
            calls.append(True)
            if len(calls) == 3:  # outer, queue attempt, successful slot grant
                project.set_control(paused=True)
            return check(project)
        with patch.object(self.engine, "_check_admission", side_effect=recheck):
            with self.assertRaises(Denied), c.reservation(self.engine, self.project, "worker"):
                pass
        self.assertEqual(len(calls), 3)
        self.assertEqual(c.pending(self.store, "worker"), original)
        # Refusal releases the execution exclusions, it doesn't lend them.
        with lock(role_exclusion(self.project.root, "worker"), blocking=False), \
                lock(self.project.root / "locks/workspace.lock", blocking=False):
            pass

    def test_full_registry_and_per_role_conflict_defer_without_overwrite(self):
        source, snapshot = self.seal()
        original = self.prepare(source, snapshot)
        other_source, other_snapshot = self.seal()
        with self.assertRaises(Busy):
            self.prepare(other_source, other_snapshot)
        self.assertEqual(c.pending(self.store, "worker"), original)
        rows = []
        for index in range(c.MAX_PENDING):
            role, run, sid = f"writer-{index}", f"{index:032x}", f"{index:064x}"
            rows.append({"role": role, "source_run": run, "snapshot": sid,
                         "successor": c._successor(role, run, sid), "state": "prepared"})
        pointer = c.pointer_at(self.store.root)
        pointer["continuations"] = {"version": 1, "entries": rows}
        self.assertEqual(len(c.entries(pointer)), c.MAX_PENDING)
        write_json(self.store.root / "current.json", pointer)
        self.assertLess((self.store.root / "current.json").stat().st_size, c.POINTER_BYTES)
        with self.assertRaisesRegex(Busy, "registry is full"):
            self.prepare(source, snapshot)
        self.assertEqual(c.entries(c.pointer_at(self.store.root)), rows)

    def test_ordinary_engine_continue_runs_once_without_preparing_intent(self):
        def seal(context, *_):
            context.handle("finish", {"outcome": "continue", "summary": "normal seal",
                                      "state": 'normal next state\nNext: Inspect the test result'})
        driver = ScriptDriver(seal)
        result = Engine(self.config, driver=driver).run(self.project, "worker")
        self.assertEqual(len(driver.calls), 1)
        self.assertEqual(result["finish"]["outcome"], "continue")
        self.assertEqual(c.entries(c.pointer_at(self.store.root)), [])
        self.assertEqual(len(list((self.store.root / "runs").iterdir())), 1)

    def test_prepared_and_reserved_intents_pin_retirement_evidence(self):
        source, snapshot = self.seal()
        self.prepare(source, snapshot)
        # This source would be old and otherwise eligible for retirement.
        write_json(self.store.root / "runs" / source / "result.json", {
            "run": source, "role": "worker", "snapshot": snapshot["id"],
            "finished_at": "2020-01-01T00:00:01+00:00", "status": "completed",
            "finish": {"outcome": "continue", "state": "state", "summary": "old"}})
        for phase in ("prepared", "reserved"):
            with self.subTest(phase=phase):
                if phase == "reserved":
                    self.store.publish(snapshot)
                    with c.reservation(self.engine, self.project, "worker"):
                        pass
                planned = storage_runs.plan(self.project)
                self.assertTrue(planned["complete"], planned)
                self.assertEqual(planned["pinned_runs"], 1)
                self.assertEqual(planned["components"], [])
        # Missing source evidence fails the whole plan closed.
        shutil.rmtree(self.store.root / "runs" / source)
        self.assertFalse(storage_runs.plan(self.project)["complete"])

    def test_missing_committed_manifest_cannot_reserve(self):
        _, snapshot = self.committed()
        # Advance the head so the source is not also the current manifest.
        _, later = self.seal(outcome="wait")
        self.store.publish(later)
        original = c.pending(self.store, "worker")
        (self.store.root / "snapshots" / (snapshot["id"] + ".json")).unlink()
        with self.assertRaises(Denied), c.reservation(self.engine, self.project, "worker"):
            pass
        self.assertEqual(c.pending(self.store, "worker"), original)


class PreparedReconciliationTests(ContinuationFixture):
    def failed_preparation(self):
        source, snapshot = self.seal()
        row = self.prepare(source, snapshot)
        directory = self.store.root / "runs" / source
        error = {"run": source, "role": "worker", "status": "interrupted",
                 "error": "publication refused", "finished_at": now(),
                 "publication": "not_published", "durability": "confirmed"}
        result = {**error, "snapshot": snapshot["id"],
                  "finish": {"outcome": "continue", "summary": "yield", "state": "state"}}
        write_json(directory / "error.json", error)
        write_json(directory / "result.json", result)
        return row, snapshot, directory

    def reconcile(self, store=None, role="worker"):
        with lock(role_exclusion(self.project.root, role)), \
                lock(self.project.root / "locks/workspace.lock"):
            return c.reconcile_prepared(store or self.store, role)

    def test_positive_cleanup_preserves_unrelated_rows_workspace_and_failure(self):
        row, _, directory = self.failed_preparation()
        other_source, other_seal = self.seal(role="reporter")
        other = self.prepare(other_source, other_seal, role="reporter")
        pointer = c.pointer_at(self.store.root)
        evidence = {p.name: p.read_bytes() for p in directory.iterdir()}
        dirty = self.project.workspace / "app.py"
        dirty.write_bytes(b"unsealed work must survive\n")
        self.assertTrue(self.reconcile())
        self.assertFalse(self.reconcile())
        self.assertEqual(c.entries(c.pointer_at(self.store.root)), [other])
        self.assertEqual(c.pointer_at(self.store.root)["snapshot"], pointer["snapshot"])
        self.assertEqual(c.pointer_at(self.store.root)["history"], pointer["history"])
        self.assertEqual(dirty.read_bytes(), b"unsealed work must survive\n")
        self.assertEqual({p.name: p.read_bytes() for p in directory.iterdir()}, evidence)
        self.assertNotEqual(row["snapshot"], self.store.get()["id"])

    def test_later_publications_and_restart_do_not_replace_exact_failure_proof(self):
        row, _, _ = self.failed_preparation()
        self.store.history_index = 1
        for _ in range(2):
            _, later = self.seal(outcome="wait")
            self.store.publish(later)
        reopened = Project(self.config, self.project.name).snapshots
        self.assertEqual(c.pending(reopened, "worker"), row)
        self.assertTrue(self.reconcile(reopened))
        self.assertEqual(reopened.get()["id"], later["id"])
        self.assertFalse(self.reconcile(reopened))

    def test_prepare_retires_old_failure_and_terminal_retry_cannot_recreate_it(self):
        _, snapshot, _ = self.failed_preparation()
        source = snapshot["run"]
        with self.assertRaisesRegex(Denied, "Terminal failed source"):
            self.prepare(source, snapshot)
        self.assertIsNone(c.pending(self.store, "worker"))
        with self.assertRaisesRegex(Denied, "Terminal failed source"):
            self.prepare(source, snapshot)
        next_source, next_seal = self.seal()
        next_row = self.prepare(next_source, next_seal)
        self.assertEqual(self.prepare(next_source, next_seal), next_row)
        self.assertFalse(self.reconcile())
        self.assertEqual(c.pending(self.store, "worker"), next_row)

    def test_error_only_terminal_source_cannot_recreate_a_retired_intent(self):
        source, snapshot = self.seal()
        directory = self.store.root / "runs" / source
        write_json(directory / "error.json", {
            "run": source, "role": "worker", "status": "interrupted",
            "error": "failed before result write", "finished_at": now(),
            "publication": "not_published", "durability": "confirmed"})
        with self.assertRaisesRegex(Denied, "Terminal failed source"):
            self.prepare(source, snapshot)
        self.assertIsNone(c.pending(self.store, "worker"))

    def test_new_prepare_cleans_a_different_failed_source_before_busy(self):
        self.failed_preparation()
        source, snapshot = self.seal()
        row = self.prepare(source, snapshot)
        self.assertEqual(c.pending(self.store, "worker"), row)
        self.assertEqual(row["source_run"], source)

    def test_missing_crashed_prepared_and_invalid_envelopes_stay_pinned(self):
        row, _, directory = self.failed_preparation()
        started = read_json(directory / "started.json")
        result = read_json(directory / "result.json")
        error = read_json(directory / "error.json")
        cases = [
            ("no terminal", started, None, None),
            ("error only", started, None, error),
            ("result only", started, result, None),
            ("prepared only", started, {**result, "status": "prepared"}, None),
            ("crash between error and result", started, {**result, "status": "prepared"}, error),
            ("wrong source", {**started, "run": uuid.uuid4().hex}, result, error),
            ("wrong role", {**started, "role": "reporter"}, result, error),
            ("wrong input", {**started, "snapshot": row["snapshot"]}, result, error),
            ("naive time", {**started, "started_at": "2020-01-01"}, result, error),
            ("future terminal", started, {**result, "finished_at": "2999-01-01T00:00:00+00:00"}, error),
            ("wrong result run", started, {**result, "run": uuid.uuid4().hex}, error),
            ("wrong result role", started, {**result, "role": "reporter"}, error),
            ("wrong output", started, {**result, "snapshot": started["snapshot"]}, error),
            ("missing output", started, {k: v for k, v in result.items() if k != "snapshot"}, error),
            ("wrong finish", started, {**result, "finish": {"outcome": "wait"}}, error),
            ("malformed finish", started, {**result, "finish": []}, error),
            ("successful result", started, {**result, "status": "completed"}, error),
            ("mismatched error", started, result, {**error, "error": "different failure"}),
            ("unconfirmed", started, {**result, "durability": "unconfirmed"},
             {**error, "durability": "unconfirmed"}),
            ("unknown publication", started, {**result, "publication": "unknown"},
             {**error, "publication": "unknown"}),
            ("published", started, {**result, "publication": "published"},
             {**error, "publication": "published"}),
            ("bounded inconsistent pair", started, {**result, "status": "bounded"},
             {**error, "status": "bounded"}),
        ]
        pointer = c.pointer_at(self.store.root)
        for label, start, res, err in cases:
            with self.subTest(label=label):
                for name, value in (("started.json", start), ("result.json", res), ("error.json", err)):
                    path = directory / name
                    path.unlink(missing_ok=True)
                    if value is not None:
                        write_json(path, value)
                self.assertFalse(self.reconcile())
                self.assertEqual(c.pointer_at(self.store.root), pointer)
        # None of these cases turns a prepared row into readiness.
        self.assertIsNone(c.ready(self.store, "worker"))
        self.assertEqual(c.pending(self.store, "worker"), row)

    def test_malformed_oversized_unreadable_and_contradictory_evidence_is_preserved(self):
        row, snapshot, directory = self.failed_preparation()
        path = directory / "result.json"
        original = path.read_bytes()
        for payload in (b"{", b"[]", b" " * (storage_runs.RECORD_BYTES + 1)):
            with self.subTest(payload_bytes=len(payload)):
                path.write_bytes(payload)
                self.assertFalse(self.reconcile())
                self.assertEqual(path.read_bytes(), payload)
        path.write_bytes(original)
        with patch.object(storage_runs.Scan, "record", side_effect=PermissionError("unreadable")):
            self.assertFalse(self.reconcile())
        write_json(directory / "delegation.json", {"status": "completed"})
        self.assertFalse(self.reconcile())
        (directory / "delegation.json").unlink()
        manifest = self.store.root / "snapshots" / (snapshot["id"] + ".json")
        manifest_bytes = manifest.read_bytes()
        write_json(manifest, {**snapshot, "run": uuid.uuid4().hex})
        self.assertFalse(self.reconcile())
        manifest.write_bytes(manifest_bytes)
        # A valid envelope is still insufficient when the immutable seal binds
        # a different run/outcome (mock only bypasses content-address validation).
        get = self.store.get
        for change in ({"run": uuid.uuid4().hex}, {"outcome": "wait"}):
            with patch.object(self.store, "get", side_effect=lambda sid=None: (
                    {**snapshot, **change} if sid == snapshot["id"] else get(sid))):
                self.assertFalse(self.reconcile())
        self.assertEqual(c.pending(self.store, "worker"), row)

    def test_active_successor_and_inconsistent_history_stay_pinned(self):
        row, _, _ = self.failed_preparation()
        active = self.store.root / "active/worker.json"
        write_json(active, {"run": row["source_run"], "role": "worker", "started_at": now()})
        self.assertFalse(self.reconcile())
        write_json(active, {})
        self.assertFalse(self.reconcile())
        active.unlink()
        successor_start = self.store.root / "runs" / row["successor"] / "started.json"
        write_json(successor_start, {"run": row["successor"]})
        self.assertFalse(self.reconcile())
        successor_start.unlink()
        successor_result = successor_start.parent / "result.json"
        write_json(successor_result, {"run": row["successor"]})
        self.assertFalse(self.reconcile())
        successor_result.unlink()
        pointer = c.pointer_at(self.store.root)
        publish_pointer(self.store.root, {**pointer, "snapshot": row["snapshot"]}, name="current.json")
        self.assertFalse(self.reconcile())
        publish_pointer(self.store.root, pointer, name="current.json")
        with patch.object(self.store, "_history_ids", return_value=[row["snapshot"], pointer["snapshot"]]):
            self.assertFalse(self.reconcile())
        self.assertEqual(c.pending(self.store, "worker"), row)

    def test_committed_and_reserved_rows_never_retire_from_source_failure(self):
        row, snapshot, _ = self.failed_preparation()
        self.store.publish(snapshot)
        self.assertFalse(self.reconcile())
        with c.reservation(self.engine, self.project, "worker"):
            pass
        self.assertFalse(self.reconcile())
        self.assertEqual(c.pending(self.store, "worker")["state"], "reserved")
        self.assertEqual(c.pending(self.store, "worker")["successor"], row["successor"])

    def test_retirement_fault_and_restart_are_idempotent(self):
        row, _, _ = self.failed_preparation()
        with patch.object(c, "publish_pointer", side_effect=OSError("before pointer")):
            with self.assertRaises(OSError):
                self.reconcile()
        self.assertEqual(c.pending(self.store, "worker"), row)
        def after_commit(*args, **kwargs):
            publish_pointer(*args, **kwargs)
            raise OSError("after pointer")
        with patch.object(c, "publish_pointer", side_effect=after_commit):
            with self.assertRaises(OSError):
                self.reconcile()
        reopened = Project(self.config, self.project.name).snapshots
        self.assertFalse(self.reconcile(reopened))
        self.assertIsNone(c.pending(reopened, "worker"))

    def test_normal_writer_attempts_cleanup_after_admission_and_can_progress(self):
        row, _, _ = self.failed_preparation()
        reconcile = c.reconcile_prepared
        attempted = []
        def cleanup(store, role):
            for exclusion in (role_exclusion(self.project.root, role),
                              self.project.root / "locks/workspace.lock"):
                with self.assertRaises(Busy), lock(exclusion, blocking=False):
                    pass
            attempted.append(role)
            return reconcile(store, role)
        driver = ScriptDriver(lambda *_: self.assertIsNone(c.pending(self.store, "worker")))
        with patch.object(c, "reconcile_prepared", side_effect=cleanup):
            result = Engine(self.config, driver=driver).run(self.project, "worker")
        self.assertEqual(attempted, ["worker"])
        self.assertNotEqual(result["run"], row["source_run"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(driver.calls), 1)

    def test_normal_writer_can_progress_without_cleaning_ambiguous_intent(self):
        source, snapshot = self.seal()
        row = self.prepare(source, snapshot)
        driver = ScriptDriver(lambda *_: self.assertEqual(c.pending(self.store, "worker"), row))
        result = Engine(self.config, driver=driver).run(self.project, "worker")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(c.pending(self.store, "worker"), row)
        self.assertEqual(len(driver.calls), 1)


class EngineContinuationTests(ContinuationFixture):
    """Real Engine/queue with a local ScriptDriver, never provider replay."""
    def quiet_head(self, *, source=None, outcome="continue"):
        # A head with no ordinary input wake or progress self-admission.
        from mizu.runtime import _progress_record
        current = self.store.get()
        progress = _progress_record(current, "worker", self.config.limits.progress_signals,
                                    {key: False for key in ("tool", "code", "state", "decision", "insight", "input")}, outcome)
        snapshot = self.store.create(self.store.capture_files(self.project.workspace),
            goal=self.project.goal, state=current["state"], run=source, outcome=outcome,
            summary="No ordinary wake", parent=current["id"], progress=progress,
            inbox_seen=self.project.insights.generation(),
            wake_generation=self.project.control().get("wake_generation", ""))
        return snapshot

    def intent_on_quiet_head(self):
        source = uuid.uuid4().hex
        write_json(self.store.root / "runs" / source / "started.json", {
            "run": source, "role": "worker", "snapshot": self.store.get()["id"]})
        snapshot = self.quiet_head(source=source)
        original = self.prepare(source, snapshot)
        return original, snapshot

    def seeded_start(self):
        original, snapshot = self.committed()
        with c.reservation(self.engine, self.project, "worker") as row:
            c.start(self.store, row, {"run": row["successor"], "role": "worker",
                                    "snapshot": snapshot["id"], "started_at": now()})
        return original, snapshot, self.store.root / "runs" / original["successor"]

    def test_only_committed_intent_bypasses_parked_progress(self):
        from mizu.runtime import should_run
        quiet = self.quiet_head()
        self.store.publish(quiet)
        self.assertFalse(should_run(self.project, quiet, role_name="worker"))
        original, snapshot = self.intent_on_quiet_head()
        self.assertFalse(should_run(self.project, quiet, role_name="worker"))
        self.store.publish(snapshot)
        self.assertTrue(should_run(self.project, snapshot, role_name="worker"))
        with c.reservation(self.engine, self.project, "worker"):
            pass
        self.assertTrue(should_run(self.project, snapshot, role_name="worker"))
        for control in ({"armed": False}, {"paused": True}, {"draining": True}):
            self.project.set_control(**control)
            self.assertFalse(should_run(self.project, snapshot, role_name="worker"))
            self.project.set_control(armed=True, paused=False, draining=False)
        with patch("mizu.runtime.check_jobs.waiting", return_value=True):
            self.assertFalse(should_run(self.project, snapshot, role_name="worker"))
        with patch("mizu.runtime.producer_hold", return_value={"reason": "held"}):
            self.assertFalse(should_run(self.project, snapshot, role_name="worker"))
        self.assertEqual(c.pending(self.store, "worker")["successor"], original["successor"])

    def test_engine_accepts_current_tree_and_identity_once(self):
        original, _ = self.committed()
        (self.project.workspace / "app.py").write_bytes(b"VALUE = 3\n")
        _, later = self.seal(outcome="wait")
        self.store.publish(later)
        def work(ctx, *_):
            self.assertEqual(ctx.run_dir.name, original["successor"])
            self.assertEqual(ctx.snapshot["id"], later["id"])
            self.assertEqual((ctx.workspace / "app.py").read_bytes(), b"VALUE = 3\n")
            started = read_json(ctx.run_dir / "started.json")
            self.assertEqual(started["continuation"], c.identity(original))
            self.assertEqual(started["snapshot"], later["id"])
            self.assertEqual(read_json(self.project.root / "active/worker.json")["run"], ctx.run_dir.name)
            self.assertEqual(c.pending(self.store, "worker")["state"], "reserved")
        driver = ScriptDriver(work)
        result = Engine(self.config, driver=driver).run(self.project, "worker", event_trigger=True)
        self.assertEqual(result["run"], original["successor"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(driver.calls), 1)
        self.assertIsNone(c.pending(self.store, "worker"))
        self.assertEqual(Engine(self.config, driver=driver).run(
            self.project, "worker", event_trigger=True)["skipped"], "no-event")
        self.assertEqual(len(driver.calls), 1)

    def test_engine_fifo_refusals_leave_committed_intent(self):
        original, _ = self.committed()
        config = dataclasses.replace(self.config, limits=dataclasses.replace(
            self.config.limits, parallel_runs=1, admission_queue_capacity=1))
        engine = Engine(config, driver=self.engine.driver)
        with contextlib.closing(_Queue(config, [role_exclusion(self.project.root, "reporter")])) as older:
            with lock(config.data / "slots/0.lock"):
                with self.assertRaises(Busy):
                    older.attempt(enqueue=True)
                with self.assertRaisesRegex(Busy, "queue is full"):
                    engine.run(self.project, "worker", retry_busy=True)
            with self.assertRaisesRegex(Busy, "Older execution admission is ready"):
                engine.run(self.project, "worker")
            self.assertEqual(c.pending(self.store, "worker")["state"], "committed")
            with older.attempt(enqueue=True):
                with self.assertRaises(Busy):
                    engine.run(self.project, "worker")
        driver = ScriptDriver()
        result = Engine(config, driver=driver).run(self.project, "worker")
        self.assertEqual(result["run"], original["successor"])
        self.assertEqual(len(driver.calls), 1)

    def test_duplicate_wake_after_another_consumer_settled_is_stale(self):
        original, _ = self.committed()
        driver = ScriptDriver()
        engine = Engine(self.config, driver=driver)
        reserve = c.reservation
        @contextlib.contextmanager
        def raced(*args, **kwargs):
            with patch.object(c, "reservation", reserve):
                first = engine.run(self.project, "worker")
                self.assertEqual(first["run"], original["successor"])
            with reserve(*args, **kwargs) as row:
                yield row
        with patch.object(c, "reservation", raced):
            self.assertEqual(engine.run(self.project, "worker")["skipped"], "stale-continuation")
        self.assertEqual(len(driver.calls), 1)

    def test_crash_before_durable_start_retries_same_empty_directory(self):
        original, _ = self.committed()
        directory = self.store.root / "runs" / original["successor"]
        directory.mkdir()
        (directory / ".new-abandoned").write_bytes(b"uncommitted start bytes")
        with patch.object(c, "start", side_effect=OSError("crash before start")):
            with self.assertRaisesRegex(OSError, "crash before start"):
                self.engine.run(self.project, "worker")
        self.assertEqual(c.pending(self.store, "worker")["successor"], original["successor"])
        self.assertFalse((directory / "started.json").exists())
        driver = ScriptDriver()
        self.assertEqual(Engine(self.config, driver=driver).run(
            self.project, "worker")["run"], original["successor"])
        self.assertEqual(len(driver.calls), 1)
        self.assertEqual((directory / ".new-abandoned").read_bytes(), b"uncommitted start bytes")

    def test_crash_after_started_before_active_pointer_never_dispatches(self):
        from mizu import runtime
        original, _ = self.committed()
        write = runtime.write_json
        def crash(path, value, **kwargs):
            if path == self.project.root / "active/worker.json":
                raise OSError("crash before active pointer")
            return write(path, value, **kwargs)
        with patch.object(runtime, "write_json", side_effect=crash):
            with self.assertRaisesRegex(OSError, "crash before active pointer"):
                self.engine.run(self.project, "worker")
        started = read_json(self.store.root / "runs" / original["successor"] / "started.json")
        self.assertEqual(started["continuation"], c.identity(original))
        restarted = Project(self.config, self.project.name)
        result = Engine(self.config, driver=self.engine.driver).run(restarted, "worker")
        self.assertEqual(result["run"], original["successor"])
        self.assertEqual(result["status"], "interrupted")
        self.assertIsNone(c.pending(self.store, "worker"))
        self.assertEqual(self.engine.driver.calls, [])

    def test_start_write_succeeds_but_caller_sees_failure(self):
        original, _ = self.committed()
        start = c.start
        def crash(*args):
            start(*args)
            raise OSError("unobserved durable start")
        with patch.object(c, "start", side_effect=crash):
            with self.assertRaises(OSError):
                self.engine.run(self.project, "worker")
        self.assertEqual(self.engine.run(self.project, "worker")["status"], "interrupted")
        self.assertEqual(self.engine.driver.calls, [])

    def test_existing_terminal_result_is_returned_byte_for_byte(self):
        original, snapshot, directory = self.seeded_start()
        output = self.store.create(self.store.capture_files(self.project.workspace),
            goal=self.store.get(snapshot["id"])["goal"], state="state",
            run=original["successor"], outcome="wait", summary="already finished",
            parent=snapshot["id"])
        self.store.publish(output)
        result = {"run": original["successor"], "role": "worker", "status": "completed",
                  "snapshot": output["id"], "finished_at": now(),
                  "finish": {"outcome": "wait", "summary": "already finished", "state": "state"}}
        write_json(directory / "result.json", result)
        before = (directory / "started.json").read_bytes(), (directory / "result.json").read_bytes()
        write_json(self.project.root / "active/worker.json", {"run": original["successor"]})
        _, later = self.seal(outcome="wait")
        self.store.publish(later)
        self.assertEqual(self.engine.run(self.project, "worker"), result)
        self.assertEqual(before, ((directory / "started.json").read_bytes(), (directory / "result.json").read_bytes()))
        self.assertFalse((self.project.root / "active/worker.json").exists())
        self.assertIsNone(c.pending(self.store, "worker"))
        self.assertTrue(c.settle(self.store, original))
        self.assertEqual(self.engine.driver.calls, [])

    def test_matching_run_terminal_with_wrong_snapshot_is_not_consumed(self):
        original, snapshot, directory = self.seeded_start()
        forged = {"run": original["successor"], "role": "worker", "status": "completed",
                  "snapshot": snapshot["id"], "finished_at": now(),
                  "finish": {"outcome": "continue", "summary": "not this run", "state": "state"}}
        write_json(directory / "result.json", forged)
        before = (directory / "result.json").read_bytes()
        with self.assertRaisesRegex(Denied, "output snapshot is missing"):
            self.engine.run(self.project, "worker")
        self.assertEqual((directory / "result.json").read_bytes(), before)
        self.assertIsNotNone(c.pending(self.store, "worker"))
        self.assertEqual(self.engine.driver.calls, [])

    def test_prepared_result_is_interrupted_not_republished(self):
        original, snapshot, directory = self.seeded_start()
        output = self.store.create(self.store.capture_files(self.project.workspace),
            goal=self.store.get(snapshot["id"])["goal"], state="state",
            run=original["successor"], outcome="continue", summary="prepared",
            parent=snapshot["id"])
        write_json(directory / "result.json", {"run": original["successor"], "role": "worker",
            "status": "prepared", "snapshot": output["id"], "finished_at": now(),
            "finish": {"outcome": "continue", "summary": "prepared", "state": "state"}})
        head = self.store.get()["id"]
        self.assertEqual(self.engine.run(self.project, "worker")["status"], "interrupted")
        self.assertEqual(self.store.get()["id"], head)
        self.assertEqual(self.engine.driver.calls, [])

    def test_settlement_failure_keeps_terminal_for_reconciliation(self):
        original, _ = self.committed()
        driver = ScriptDriver()
        engine = Engine(self.config, driver=driver)
        with patch.object(c, "settle", side_effect=OSError("pointer write failed")):
            terminal = engine.run(self.project, "worker")
        self.assertEqual(c.pending(self.store, "worker")["successor"], original["successor"])
        self.assertEqual(engine.run(self.project, "worker"), terminal)
        self.assertEqual(len(driver.calls), 1)
        self.assertIsNone(c.pending(self.store, "worker"))

    def test_recovery_result_write_failure_keeps_same_start_and_intent(self):
        original, _, directory = self.seeded_start()
        before = (directory / "started.json").read_bytes()
        with patch.object(c, "write_json", side_effect=OSError("recovery record failed")):
            with self.assertRaises(OSError):
                self.engine.run(self.project, "worker")
        self.assertEqual((directory / "started.json").read_bytes(), before)
        self.assertEqual(c.pending(self.store, "worker")["successor"], original["successor"])
        self.assertEqual(self.engine.run(self.project, "worker")["status"], "interrupted")
        self.assertEqual(self.engine.driver.calls, [])

    def test_mismatched_existing_evidence_is_never_overwritten(self):
        original, _, directory = self.seeded_start()
        started = read_json(directory / "started.json")
        started["continuation"]["source_run"] = "0" * 32
        write_json(directory / "started.json", started)
        before = (directory / "started.json").read_bytes()
        with self.assertRaisesRegex(Denied, "start binding mismatch"):
            self.engine.run(self.project, "worker")
        self.assertEqual((directory / "started.json").read_bytes(), before)
        self.assertIsNotNone(c.pending(self.store, "worker"))
        self.assertEqual(self.engine.driver.calls, [])

    def test_dirty_workspace_refuses_before_start_without_discarding_files(self):
        original, _ = self.committed()
        (self.project.workspace / "app.py").write_bytes(b"uncommitted change\n")
        with self.assertRaisesRegex(Denied, "published workspace tree"):
            self.engine.run(self.project, "worker")
        self.assertEqual((self.project.workspace / "app.py").read_bytes(), b"uncommitted change\n")
        self.assertFalse((self.store.root / "runs" / original["successor"]).exists())
        self.assertEqual(c.pending(self.store, "worker")["successor"], original["successor"])

    def test_engine_holds_preserve_same_row_without_start(self):
        original, _ = self.committed()
        initial = c.pending(self.store, "worker")
        self.engine.stop.set()
        with self.assertRaises(Busy):
            self.engine.run(self.project, "worker")
        self.engine.stop.clear()
        for control in ({"armed": False}, {"paused": True}, {"draining": True}):
            self.project.set_control(**control)
            with self.assertRaises((Busy, Denied)):
                self.engine.run(self.project, "worker")
            self.project.set_control(armed=True, paused=False, draining=False)
        limited = dataclasses.replace(self.config, limits=dataclasses.replace(self.config.limits, daily_requests=0))
        with self.assertRaises(InfraExceeded):
            Engine(limited).run(self.project, "worker")
        with patch("mizu.runtime.producer_hold", return_value={"reason": "held"}):
            self.assertEqual(self.engine.run(self.project, "worker")["skip"]["skipped"], "producer-held")
        with patch("mizu.runtime.check_jobs.deferral", return_value={"id": "a" * 64, "wait_until": 9999999999}):
            with self.assertRaises(Busy):
                role = dataclasses.replace(self.config.roles["worker"], capabilities=(
                    *self.config.roles["worker"].capabilities, "check_job"))
                config = dataclasses.replace(self.config, roles={**self.config.roles, "worker": role})
                Engine(config, driver=self.engine.driver).run(self.project, "worker")
        self.assertEqual(c.pending(self.store, "worker"), initial)
        self.assertFalse((self.store.root / "runs" / original["successor"]).exists())

    def test_prepared_never_bypasses_event_dispatch(self):
        quiet = self.quiet_head()
        self.store.publish(quiet)
        original, _ = self.intent_on_quiet_head()
        self.assertEqual(self.engine.run(self.project, "worker", event_trigger=True)["skipped"], "no-event")
        self.assertEqual(c.pending(self.store, "worker")["state"], "prepared")
        self.assertFalse((self.store.root / "runs" / original["successor"]).exists())
        self.assertEqual(self.engine.driver.calls, [])

    def test_live_start_cannot_be_recovered_during_slot_releasing_retry(self):
        original, _ = self.committed()
        admitted_stacks = []
        def work(ctx, *_):
            # Same release/re-admit path used by selector retry waits. The
            # separate invocation owner must outlive the admission stack.
            stack = admitted_stacks[-1]
            stack.close()
            ctx._slot_lease = False
            with self.assertRaises(Busy):
                self.engine.run(self.project, "worker")
            self.assertFalse((ctx.run_dir / "result.json").exists())
            self.assertEqual(c.pending(self.store, "worker")["successor"], original["successor"])
            engine._selection_retry_wait(ctx, self.project, stack, ctx.role, "worker", 0)
        driver = ScriptDriver(work)
        engine = Engine(self.config, driver=driver)
        admit = engine._admit
        def record(project, name, role, stack, **kwargs):
            admitted_stacks.append(stack)
            return admit(project, name, role, stack, **kwargs)
        with patch.object(engine, "_admit", side_effect=record):
            self.assertEqual(engine.run(self.project, "worker")["status"], "completed")
        self.assertEqual(len(driver.calls), 1)
        self.assertEqual(self.engine.driver.calls, [])
        self.assertIsNone(c.pending(self.store, "worker"))

    def test_failed_atomic_start_write_does_not_create_error_or_consume(self):
        original, _ = self.committed()
        initial = c.pending(self.store, "worker")
        with patch.object(c, "write_json", side_effect=OSError("start write failed")):
            with self.assertRaisesRegex(OSError, "start write failed"):
                self.engine.run(self.project, "worker")
        directory = self.store.root / "runs" / original["successor"]
        self.assertEqual(list(directory.iterdir()), [])
        self.assertEqual(c.identity(c.pending(self.store, "worker")), c.identity(initial))
        driver = ScriptDriver()
        self.assertEqual(Engine(self.config, driver=driver).run(
            self.project, "worker")["run"], original["successor"])
        self.assertEqual(len(driver.calls), 1)

    def test_ordinary_continue_runs_once_without_preparation_or_reservation(self):
        def work(ctx, *_):
            ctx.handle("finish", {"outcome": "continue", "summary": "One execution", "state": 'Next\nNext: Inspect the test result'})
        driver = ScriptDriver(work)
        with patch.object(c, "reservation", side_effect=AssertionError("ordinary admission only")):
            result = Engine(self.config, driver=driver).run(self.project, "worker")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["finish"]["outcome"], "continue")
        self.assertEqual(len(driver.calls), 1)
        self.assertIsNone(c.pending(self.store, "worker"))
        self.assertNotIn("continuation", read_json(self.store.root / "runs" / result["run"] / "started.json"))

    def test_unbound_directory_and_wrong_terminal_are_not_overwritten(self):
        original, _ = self.committed()
        directory = self.store.root / "runs" / original["successor"]
        write_json(directory / "result.json", {"run": "a" * 32, "role": "worker", "status": "completed"})
        before = (directory / "result.json").read_bytes()
        with self.assertRaisesRegex(Denied, "without a durable start"):
            self.engine.run(self.project, "worker")
        self.assertEqual((directory / "result.json").read_bytes(), before)
        self.assertIsNotNone(c.pending(self.store, "worker"))
        (directory / "result.json").unlink()
        with c.reservation(self.engine, self.project, "worker") as row:
            c.start(self.store, row, {"run": row["successor"], "role": "worker",
                                    "snapshot": row["current_snapshot"], "started_at": now()})
        write_json(directory / "result.json", {"run": "a" * 32, "role": "worker", "status": "completed"})
        with self.assertRaisesRegex(Denied, "result binding mismatch"):
            self.engine.run(self.project, "worker")
        self.assertEqual((directory / "result.json").read_bytes(), before)
        self.assertEqual(self.engine.driver.calls, [])


class ChainedContinuationTests(ContinuationFixture):
    """One executing root plus one next yield; no scheduler yield producer."""

    def running_parent(self):
        self.committed()
        with c.reservation(self.engine, self.project, "worker") as parent:
            c.start(self.store, parent, {
                "run": parent["successor"], "role": "worker",
                "snapshot": parent["current_snapshot"],
                "started_at": "2020-01-01T00:00:00+00:00"})
        return parent

    def next_seal(self, parent):
        current = self.store.get()
        return self.store.create(self.store.capture_files(self.project.workspace),
            goal=current["goal"], state="state", run=parent["successor"],
            outcome="continue", summary="next yield", parent=current["id"])

    def child(self, parent, *, commit=True):
        seal = self.next_seal(parent)
        child = self.prepare(parent["successor"], seal)
        if commit:
            self.store.publish(seal)
        return child, seal

    def terminal(self, parent, seal):
        result = {"run": parent["successor"], "role": "worker", "status": "completed",
                  "finished_at": "2020-01-01T00:00:01+00:00",
                  "snapshot": seal["id"],
                  "continuation": c.identity(parent)}
        result["finish"] = {"outcome": "continue", "summary": "next yield", "state": "state"}
        write_json(self.store.root / "runs" / parent["successor"] / "result.json", result)
        return result

    def rows(self, store=None):
        return c.entries(c.pointer_at((store or self.store).root))

    def reverse_rows(self):
        pointer = c.pointer_at(self.store.root)
        pointer["continuations"]["entries"].reverse()
        write_json(self.store.root / "current.json", pointer)

    def test_child_creation_and_replay_leave_parent_first_even_in_reverse_disk_order(self):
        parent = self.running_parent()
        child, seal = self.child(parent, commit=False)
        self.assertEqual(c.MAX_PER_ROLE, 2)
        self.assertEqual(child["source_run"], parent["successor"])
        self.assertEqual(self.prepare(child["source_run"], seal), child)
        self.reverse_rows()
        self.assertEqual([c.identity(r) for r in self.rows()],
                         [c.identity(parent), c.identity(child)])
        self.assertEqual(c.identity(c.ready(self.store, "worker")), c.identity(parent))
        self.store.publish(seal)
        committed_child = {**child, "state": "committed"}
        self.assertEqual(self.prepare(child["source_run"], seal), committed_child)
        # Replaying the original parent preparation must not conflict with its child.
        self.assertEqual(self.prepare(parent["source_run"], self.store.get(parent["snapshot"])),
                         {key: parent[key] for key in c._FIELDS})
        with patch.object(self.engine, "_admit", side_effect=AssertionError("child admitted")):
            with c.reservation(self.engine, self.project, "worker", expected=child) as stale:
                self.assertIsNone(stale)
        self.assertEqual(c.identity(c.pending(self.store, "worker")), c.identity(parent))

    def test_only_atomic_terminal_parent_settlement_exposes_committed_child(self):
        parent = self.running_parent()
        child, seal = self.child(parent)
        self.reverse_rows()
        self.assertFalse(c.settle(self.store, parent))
        self.assertEqual(c.identity(c.ready(self.store, "worker")), c.identity(parent))
        self.terminal(parent, seal)
        directory = self.store.root / "runs" / child["successor"]
        with self.assertRaises(Denied):
            c.recover(self.store, child)
        with self.assertRaises(Denied):
            c.start(self.store, {**child, "current_snapshot": seal["id"]}, {
                "run": child["successor"], "role": "worker", "snapshot": seal["id"],
                "started_at": now()})
        self.assertFalse(directory.exists())
        self.assertTrue(c.settle(self.store, parent))
        self.assertEqual(c.ready(self.store, "worker"), {**child, "state": "committed"})
        self.assertTrue(c.settle(self.store, parent))  # Exact identity, not first role match.
        self.assertEqual(len(self.rows()), 1)
        with c.reservation(self.engine, self.project, "worker", expected=parent) as stale:
            self.assertIsNone(stale)
        with c.reservation(self.engine, self.project, "worker", expected=child) as reserved:
            self.assertEqual(c.identity(reserved), c.identity(child))

    def test_malformed_parent_terminal_cannot_expose_child(self):
        parent = self.running_parent()
        _, seal = self.child(parent)
        result = self.terminal(parent, seal)
        path = self.store.root / "runs" / parent["successor"] / "result.json"
        pointer = c.pointer_at(self.store.root)
        for changed in ({"run": uuid.uuid4().hex}, {"snapshot": "a" * 64},
                        {"continuation": {}}, {"finished_at": "not a time"}):
            with self.subTest(changed=changed):
                write_json(path, {**result, **changed})
                with self.assertRaises(Denied):
                    c.settle(self.store, parent)
                self.assertEqual(c.pointer_at(self.store.root), pointer)

    def test_settlement_faults_before_and_after_commit_restart_without_child_loss(self):
        parent = self.running_parent()
        child, seal = self.child(parent)
        self.terminal(parent, seal)
        original = c.pointer_at(self.store.root)
        with patch.object(c, "publish_pointer", side_effect=OSError("before replacement")):
            with self.assertRaises(OSError):
                c.settle(self.store, parent)
        self.assertEqual(c.pointer_at(self.store.root), original)
        restarted = Project(self.config, self.project.name).snapshots
        self.assertEqual(c.identity(c.ready(restarted, "worker")), c.identity(parent))
        def after_commit(*args, **kwargs):
            publish_pointer(*args, **kwargs)
            raise OSError("after replacement")
        with patch.object(c, "publish_pointer", side_effect=after_commit):
            with self.assertRaises(OSError):
                c.settle(restarted, parent)
        reopened = Project(self.config, self.project.name).snapshots
        self.assertEqual(c.ready(reopened, "worker"), {**child, "state": "committed"})
        self.assertTrue(c.settle(reopened, parent))
        self.assertEqual(len(self.rows(reopened)), 1)

    def test_child_preparation_pointer_faults_are_idempotent(self):
        parent = self.running_parent()
        seal = self.next_seal(parent)
        original = c.pointer_at(self.store.root)
        with patch.object(c, "publish_pointer", side_effect=OSError("before replacement")):
            with self.assertRaises(OSError):
                self.prepare(parent["successor"], seal)
        self.assertEqual(c.pointer_at(self.store.root), original)
        def after_commit(*args, **kwargs):
            publish_pointer(*args, **kwargs)
            raise OSError("after replacement")
        with patch.object(c, "publish_pointer", side_effect=after_commit):
            with self.assertRaises(OSError):
                self.prepare(parent["successor"], seal)
        restarted = Project(self.config, self.project.name).snapshots
        child = self.rows(restarted)[1]
        self.assertEqual(c.prepare(restarted, role="worker", source_run=parent["successor"],
                                   snapshot=seal["id"]), child)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(c.identity(c.ready(restarted, "worker")), c.identity(parent))

    def test_crash_before_child_publication_recovers_parent_but_never_readies_prepared_child(self):
        parent = self.running_parent()
        child, seal = self.child(parent, commit=False)
        with patch.object(snapshot_module, "publish_pointer", side_effect=OSError("before commit")):
            with self.assertRaises(OSError):
                self.store.publish(seal)
        restarted = Project(self.config, self.project.name)
        with c.reservation(Engine(self.config), restarted, "worker") as root:
            self.assertEqual(c.identity(root), c.identity(parent))
            self.assertEqual(c.recover(restarted.snapshots, root)["status"], "interrupted")
        self.assertEqual(c.pending(restarted.snapshots, "worker"), child)
        self.assertIsNone(c.ready(restarted.snapshots, "worker"))
        with c.reservation(Engine(self.config), restarted, "worker") as missing:
            self.assertIsNone(missing)

    def test_crash_after_child_publication_recovers_parent_then_reserves_child_once(self):
        parent = self.running_parent()
        child, seal = self.child(parent, commit=False)
        def after_commit(*args, **kwargs):
            publish_pointer(*args, **kwargs)
            raise OSError("after commit")
        with patch.object(snapshot_module, "publish_pointer", side_effect=after_commit):
            with self.assertRaises(OSError):
                self.store.publish(seal)
        restarted = Project(self.config, self.project.name)
        with c.reservation(Engine(self.config), restarted, "worker") as root:
            self.assertEqual(c.identity(root), c.identity(parent))
            c.recover(restarted.snapshots, root)
        self.assertEqual(c.ready(restarted.snapshots, "worker"), {**child, "state": "committed"})
        errors = []
        def competing_wake():
            try:
                with c.reservation(Engine(self.config), restarted, "worker"):
                    errors.append("unexpected admission")
            except Busy:
                errors.append("busy")
        with c.reservation(Engine(self.config), restarted, "worker") as root:
            self.assertEqual(c.identity(root), c.identity(child))
            thread = threading.Thread(target=competing_wake)
            thread.start()
            thread.join(60)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, ["busy"])
            c.start(restarted.snapshots, root, {
                "run": child["successor"], "role": "worker", "snapshot": seal["id"],
                "started_at": now()})
        with c.reservation(Engine(self.config), restarted, "worker") as root:
            self.assertEqual(c.recover(restarted.snapshots, root)["status"], "interrupted")
        self.assertEqual(self.rows(), [])

    def test_child_requires_reserved_exact_and_durably_started_parent(self):
        original, _ = self.committed()
        seal = self.next_seal(original)
        with self.assertRaises(Busy):
            self.prepare(original["successor"], seal)
        with c.reservation(self.engine, self.project, "worker") as parent:
            pass
        with self.assertRaisesRegex(Denied, "durably started"):
            self.prepare(parent["successor"], seal)
        write_json(self.store.root / "runs" / parent["successor"] / "started.json", {
            "run": parent["successor"], "role": "worker", "snapshot": parent["current_snapshot"],
            "started_at": now(), "continuation": {}})
        with self.assertRaisesRegex(Denied, "start binding"):
            self.prepare(parent["successor"], seal)
        unrelated_source, unrelated_seal = self.seal()
        with self.assertRaises(Busy):
            self.prepare(unrelated_source, unrelated_seal)
        self.assertEqual(len(self.rows()), 1)

    def test_full_two_row_chain_refuses_another_intent_without_overwrite(self):
        parent = self.running_parent()
        child, seal = self.child(parent)
        original = c.pointer_at(self.store.root)
        for source in (child["source_run"], child["successor"], uuid.uuid4().hex):
            different = self.next_seal({"successor": source})
            with self.assertRaises(Busy):
                self.prepare(source, different)
        self.assertEqual(c.pointer_at(self.store.root), original)
        self.assertEqual(self.prepare(child["source_run"], seal)["state"], "committed")

    def test_malformed_unrelated_and_deep_chain_metadata_fail_closed(self):
        parent = self.running_parent()
        child, _ = self.child(parent)
        parent = {key: parent[key] for key in c._FIELDS}
        child = {**child, "state": "committed"}
        def changed(row, **updates):
            row = {**row, **updates}
            row["successor"] = c._successor(row["role"], row["source_run"], row["snapshot"])
            return row
        grandchild = changed(child, source_run=child["successor"])
        invalid = [
            [parent, changed(child, source_run=uuid.uuid4().hex)],
            [parent, changed(child, role="reporter")],
            [{**parent, "state": "committed"}, child],
            [{**parent, "state": "prepared"}, child],
            [parent, {**child, "state": "reserved"}],
            [parent, {**child, "extra": True}],
            [parent, {**child, "successor": uuid.uuid4().hex}],
            [parent, child, grandchild],
        ]
        self.assertEqual(c.entries({"continuations": {"version": 1, "entries": [child, parent]}}),
                         [parent, child])
        pointer = c.pointer_at(self.store.root)
        for rows in invalid:
            with self.subTest(rows=rows):
                malformed = {**pointer, "continuations": {"version": 1, "entries": rows}}
                with self.assertRaises(Denied):
                    c.entries(malformed)
                write_json(self.store.root / "current.json", malformed)
                with self.assertRaises(Denied):
                    c.ready(self.store, "worker")
                self.assertFalse(storage_runs.plan(self.project)["complete"])
        write_json(self.store.root / "current.json", pointer)

    def test_parent_retirement_allows_the_next_bounded_pair(self):
        parent = self.running_parent()
        child, seal = self.child(parent)
        self.terminal(parent, seal)
        self.assertTrue(c.settle(self.store, parent))
        with c.reservation(self.engine, self.project, "worker") as reserved:
            c.start(self.store, reserved, {
                "run": reserved["successor"], "role": "worker", "snapshot": seal["id"],
                "started_at": "2020-01-01T00:00:00+00:00"})
        grandchild, next_seal = self.child(reserved)
        self.assertEqual(len(self.rows()), c.MAX_PER_ROLE)
        self.assertEqual(c.identity(c.ready(self.store, "worker")), c.identity(child))
        # Even an old-parent settlement replay must not consume either new row.
        self.assertTrue(c.settle(self.store, parent))
        self.terminal(reserved, next_seal)
        self.assertTrue(c.settle(self.store, reserved))
        self.assertEqual(c.ready(self.store, "worker"), {**grandchild, "state": "committed"})

    def test_chain_retention_pins_both_seals_and_all_three_run_identities(self):
        parent = self.running_parent()
        child, seal = self.child(parent)
        self.terminal(parent, seal)
        write_json(self.store.root / "runs" / parent["source_run"] / "result.json", {
            "run": parent["source_run"], "role": "worker", "snapshot": parent["snapshot"],
            "status": "completed", "finished_at": "2020-01-01T00:00:01+00:00",
            "finish": {"outcome": "continue", "summary": "yield", "state": "state"}})
        with c.reservation(self.engine, self.project, "worker") as root:
            self.assertEqual(c.identity(root), c.identity(parent))
        # An existing third run directory must be pinned even before it is runnable.
        write_json(self.store.root / "runs" / child["successor"] / "started.json", {
            "run": child["successor"], "role": "worker", "snapshot": seal["id"],
            "started_at": "2020-01-01T00:00:00+00:00"})
        write_json(self.store.root / "runs" / child["successor"] / "result.json", {
            "run": child["successor"], "role": "worker", "snapshot": seal["id"],
            "status": "interrupted", "error": "interrupted", "finished_at": "2020-01-01T00:00:01+00:00"})
        self.store.history_index = 1
        _, later = self.seal(outcome="wait", role="reporter")
        self.store.publish(later)
        self.assertNotIn(parent["snapshot"], self.store._history_ids())
        self.assertNotIn(seal["id"], self.store._history_ids())
        planned = storage_runs.plan(self.project)
        self.assertTrue(planned["complete"], planned)
        self.assertEqual(planned["pinned_runs"], 3)
        self.assertEqual(planned["components"], [])
        self.assertEqual(audit(self.project)["snapshots"]["live"], 3)

    def test_prepared_child_failure_cleanup_preserves_parent_and_exact_failure(self):
        parent = self.running_parent()
        child, seal = self.child(parent, commit=False)
        directory = self.store.root / "runs" / child["source_run"]
        error = {"run": child["source_run"], "role": "worker", "status": "interrupted",
                 "error": "publication refused", "finished_at": now(),
                 "publication": "not_published", "durability": "confirmed"}
        write_json(directory / "error.json", error)
        write_json(directory / "result.json", {**error, "snapshot": seal["id"],
            "finish": {"outcome": "continue", "summary": "next yield", "state": "state"}})
        original = c.pointer_at(self.store.root)
        evidence = {p.name: p.read_bytes() for p in directory.iterdir()}
        with patch.object(c, "publish_pointer", side_effect=OSError("before cleanup")):
            with self.assertRaises(OSError):
                c.reconcile_prepared(self.store, "worker")
        self.assertEqual(c.pointer_at(self.store.root), original)
        def after_commit(*args, **kwargs):
            publish_pointer(*args, **kwargs)
            raise OSError("after cleanup")
        with patch.object(c, "publish_pointer", side_effect=after_commit):
            with self.assertRaises(OSError):
                c.reconcile_prepared(self.store, "worker")
        restarted = Project(self.config, self.project.name).snapshots
        self.assertFalse(c.reconcile_prepared(restarted, "worker"))
        self.assertEqual([c.identity(r) for r in self.rows(restarted)], [c.identity(parent)])
        self.assertEqual({p.name: p.read_bytes() for p in directory.iterdir()}, evidence)
        with self.assertRaisesRegex(Denied, "Terminal failed source"):
            self.prepare(child["source_run"], seal)
        self.assertEqual(c.recover(restarted, parent)["status"], "interrupted")
        self.assertEqual(self.rows(), [])
        with self.assertRaisesRegex(Denied, "Terminal failed source"):
            self.prepare(child["source_run"], seal)

    def test_ambiguous_prepared_child_failure_stays_pinned_through_parent_recovery(self):
        parent = self.running_parent()
        child, seal = self.child(parent, commit=False)
        directory = self.store.root / "runs" / child["source_run"]
        write_json(directory / "result.json", {
            "run": child["source_run"], "role": "worker", "status": "prepared",
            "snapshot": seal["id"], "finished_at": now(),
            "finish": {"outcome": "continue", "summary": "next yield", "state": "state"}})
        self.assertFalse(c.reconcile_prepared(self.store, "worker"))
        c.recover(self.store, parent)
        self.assertEqual(c.pending(self.store, "worker"), child)
        self.assertIsNone(c.ready(self.store, "worker"))
        self.assertFalse(c.reconcile_prepared(self.store, "worker"))

    def test_terminal_child_evidence_cannot_settle_a_nonroot_identity(self):
        parent = self.running_parent()
        child, seal = self.child(parent)
        directory = self.store.root / "runs" / child["successor"]
        write_json(directory / "started.json", {
            "run": child["successor"], "role": "worker", "snapshot": seal["id"],
            "started_at": "2020-01-01T00:00:00+00:00", "continuation": c.identity(child)})
        write_json(directory / "result.json", {
            "run": child["successor"], "role": "worker", "snapshot": seal["id"],
            "status": "interrupted", "error": "interrupted", "finished_at": "2020-01-01T00:00:01+00:00",
            "continuation": c.identity(child)})
        self.reverse_rows()
        original = c.pointer_at(self.store.root)
        with self.assertRaisesRegex(Denied, "settlement binding"):
            c.settle(self.store, child)
        self.assertEqual(c.pointer_at(self.store.root), original)

    def test_real_engine_repeated_host_preparations_settle_only_the_exact_parent(self):
        original, _ = self.committed()
        prepared = []
        create = self.store.create
        def host_prepare(*args, **kwargs):
            # Test-only future-producer seam: prepare the retained Engine seal
            # before publication. No ordinary continue is changed in production.
            seal = create(*args, **kwargs)
            if len(prepared) < 2:
                prepared.append(c.prepare(self.store, role="worker", source_run=seal["run"],
                                          snapshot=seal["id"]))
            return seal
        def work(context, *_):
            context.handle("finish", {"outcome": "continue", "summary": "seal", "state": 'state\nNext: Inspect the test result'})
        driver = ScriptDriver(work)
        engine = Engine(self.config, driver=driver)
        with patch.object(self.store, "create", side_effect=host_prepare):
            first = engine.run(self.project, "worker")
            self.assertEqual(first["run"], original["successor"])
            self.assertEqual(c.ready(self.store, "worker"), {**prepared[0], "state": "committed"})
            second = engine.run(self.project, "worker")
            self.assertEqual(second["run"], prepared[0]["successor"])
            self.assertEqual(c.ready(self.store, "worker"), {**prepared[1], "state": "committed"})
            third = engine.run(self.project, "worker")
            self.assertEqual(third["run"], prepared[1]["successor"])
        self.assertEqual(len(driver.calls), 3)
        self.assertEqual(self.rows(), [])  # Last ordinary continue made no intent.
