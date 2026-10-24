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
                                      "state": "normal next state"})
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
            ctx.handle("finish", {"outcome": "continue", "summary": "One execution", "state": "Next"})
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
