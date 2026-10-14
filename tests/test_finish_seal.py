"""Irreversible host completion with offline fake drivers, not live settlement."""
import dataclasses
import errno
import os
import time
from unittest.mock import patch

from support import Fixture, ScriptDriver
from test_selection import SelectionFixture
from mizu.budget import Budget
from mizu.errors import Cancelled, Denied, LimitExceeded, ModelFailure, ProtocolError
from mizu.fs import read_json, write_json
from mizu.runtime import Engine
from mizu.selection import State


FINISH = {"outcome": "wait", "summary": "Exact saved work", "state": "Exact saved state", "wait_seconds": 13}


class FinishSealTests(Fixture):
    def test_seal_refuses_work_before_admission_counters_or_dispatch(self):
        ctx = self.context()
        ctx.model_evidence = {"requests": 0, "admission_status": "unknown"}
        ctx.handle("_budget", {"sequence": 1})
        ctx.handle("finish", FINISH)
        sealed = read_json(ctx.run_dir / "finish-intent.json")
        admission = (ctx.run_dir / "admission.json").read_bytes()
        evidence = dict(ctx.model_evidence)
        counters = (ctx.request_count, ctx.tool_count, ctx.wrap_up_requests, ctx.wrap_up_tools)
        sequences = set(ctx.request_sequences)
        operations = (("_budget", {"sequence": 1}), ("_budget", {"sequence": 2}),
                      ("_budget", {"sequence": "invalid"}), ("_engine_tool", {"name": "ungranted"}),
                      ("_hello", {}), ("read", {"path": "app.py"}), ("exec", {"script": "true"}),
                      ("report", {"summary": "late"}), ("finish", FINISH))
        with patch("mizu.runtime.Budget.take") as take, patch.object(ctx, "dispatch") as dispatch:
            for op, args in operations:
                with self.subTest(op=op, args=args), self.assertRaises(Denied):
                    ctx.handle(op, args)
            take.assert_not_called()
            dispatch.assert_not_called()
        ctx.handle("_model_usage", {"sequence": 1, "usage": {"input": 3, "output": 2}})
        self.assertEqual(len(ctx.runtime_usage), 1)  # Evidence only, never admission or another seal.
        self.assertEqual(ctx.finished, sealed)
        self.assertEqual(read_json(ctx.run_dir / "finish-intent.json"), sealed)
        self.assertEqual((ctx.run_dir / "admission.json").read_bytes(), admission)
        self.assertEqual(ctx.model_evidence, evidence)
        self.assertEqual((ctx.request_count, ctx.tool_count, ctx.wrap_up_requests, ctx.wrap_up_tools), counters)
        self.assertEqual(ctx.request_sequences, sequences)
        self.assertIsNone(ctx.admission_error)
        self.assertIsNone(ctx.bound_hit)
        self.assertFalse(ctx.admission_wait)
        self.assertFalse(ctx.hello.is_set())
        driver = ScriptDriver()
        with self.assertRaises(Denied):
            Engine(self.config)._execute(ctx, driver, "late")
        self.assertEqual(driver.calls, [])

    def test_failed_validation_does_not_seal(self):
        for args in ({**FINISH, "summary": " "}, {**FINISH, "state": " "},
                     {**FINISH, "outcome": "done"}, {**FINISH, "outcome": "invalid"}):
            with self.subTest(args=args):
                ctx = self.context()
                with self.assertRaises(Denied):
                    ctx.handle("finish", args)
                self.assertIsNone(ctx.finished)
                self.assertFalse((ctx.run_dir / "finish-intent.json").exists())
                ctx.handle("_budget", {"sequence": 1})
                self.assertEqual(ctx.request_count, 1)

    def test_failed_finish_intent_write_does_not_seal(self):
        for persisted in (False, True):
            with self.subTest(persisted=persisted):
                ctx = self.context()
                def failing_write(path, value):
                    self.assertIsNone(ctx.finished)  # No acceptance before durability is confirmed.
                    if persisted:
                        write_json(path, value)
                    raise OSError(errno.EIO, "injected finish persistence fault")
                with patch("mizu.runtime.write_json", side_effect=failing_write):
                    with self.assertRaises(OSError):
                        ctx.handle("finish", FINISH)
                self.assertIsNone(ctx.finished)
                ctx.handle("_budget", {"sequence": 1})
                ctx.handle("finish", FINISH)
                self.assertEqual(read_json(ctx.run_dir / "finish-intent.json"), ctx.finished)

    def test_finish_then_denied_budget_retains_exact_work_without_failure(self):
        config = dataclasses.replace(self.config, limits=dataclasses.replace(self.config.limits, max_failures=1))
        sealed = {}
        def work(ctx, *_):
            (ctx.workspace / "app.py").write_bytes(b"VALUE = 9\n")
            ctx.model_evidence = {"profile": ctx.role.profile, "engine": "pi", "requests": ctx.request_count,
                                  "admission_status": "accepted", "usage": [{"input": 3, "output": 2}],
                                  "usage_known": True}
            ctx.handle("finish", FINISH)
            sealed.update(read_json(ctx.run_dir / "finish-intent.json"))
            ctx.handle("_model_usage", {"sequence": 1, "usage": {"input": 3, "output": 2}})
            try:
                ctx.handle("_budget", {"sequence": 2})
            finally:
                ctx.cancel_operations()  # Bridge teardown is not an explicit host stop.
        driver = ScriptDriver(work)
        with patch("mizu.engine_config.write_prompt_state") as session_cursor:
            result = Engine(config, driver=driver).run(self.project, "worker")
        session_cursor.assert_not_called()
        ctx = driver.calls[0][0]
        self.assertEqual(len(driver.calls), 1)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["finish"], sealed)
        self.assertEqual(ctx.finished, sealed)
        self.assertEqual((ctx.request_count, ctx.tool_count), (1, 1))
        self.assertIsNone(ctx.admission_error)
        self.assertEqual(read_json(ctx.run_dir / "admission.json"), {"requests": 1})
        self.assertEqual(Budget(config.data / "budget", 100).usage(self.project.name)["used"], 1)
        snapshot = self.project.snapshots.get()
        self.assertEqual(snapshot["id"], result["snapshot"])
        for key in ("summary", "state", "outcome"):
            self.assertEqual(snapshot[key], sealed[key])
        self.assertEqual(snapshot["code_digest"], self.project.snapshots.capture_files(ctx.workspace)["code_digest"])
        self.assertFalse(result["model"]["settled"])
        self.assertTrue(result["model"]["usage_known"])
        self.assertEqual(result["model"]["requests"], 1)
        self.assertEqual(result["model"]["settlement"]["error_type"], "Denied")
        self.assertEqual(read_json(ctx.run_dir / "adapter-settlement.json"), result["model"]["settlement"])
        self.assertEqual(read_json(ctx.run_dir / "result.json"), result)
        self.assertFalse((ctx.run_dir / "error.json").exists())
        self.assertEqual(read_json(self.project.root / "health/worker.json")["consecutive_failures"], 0)
        self.assertFalse(self.project.control()["paused"])

    def test_sealed_eof_and_deadline_errors_do_not_undo_completion(self):
        for fault in (EOFError("x" * 5000), ProtocolError("transport EOF"),
                      LimitExceeded("run deadline"), Cancelled("deadline cancellation")):
            with self.subTest(fault=type(fault).__name__):
                def work(ctx, *_):
                    ctx.handle("finish", FINISH)
                    if isinstance(fault, (LimitExceeded, Cancelled)):
                        ctx.deadline = time.monotonic() - 1  # Synthetic expired adapter deadline.
                    raise fault
                driver = ScriptDriver(work)
                result = Engine(self.config, driver=driver).run(self.project, "worker")
                ctx = driver.calls[0][0]
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["finish"], read_json(ctx.run_dir / "finish-intent.json"))
                self.assertFalse(result["model"]["settled"])
                self.assertEqual(result["model"]["settlement"]["error_type"], type(fault).__name__)
                self.assertLessEqual(len(result["model"]["settlement"]["error"]), 4000)
                self.assertEqual(read_json(self.project.root / "health/worker.json")["consecutive_failures"], 0)
                self.assertFalse(self.project.control()["paused"])

    def test_explicit_host_cancellation_after_seal_still_cancels(self):
        for action in ("stop", "pause", "unarm"):
            for driver_error in (False, True):
                with self.subTest(action=action, driver_error=driver_error):
                    self.project.set_control(armed=True, paused=False)
                    before = self.project.snapshots.get()["id"]
                    engine = Engine(self.config)
                    def work(ctx, *_):
                        ctx.handle("finish", FINISH)
                        if action == "stop":
                            engine.stop.set()
                        else:
                            self.project.set_control(**({"paused": True} if action == "pause" else {"armed": False}))
                        if driver_error:
                            raise ProtocolError("settlement interrupted by host")
                    driver = ScriptDriver(work)
                    engine.driver = driver
                    with self.assertRaises(Cancelled):
                        engine.run(self.project, "worker")
                    ctx = driver.calls[0][0]
                    self.assertIsNotNone(ctx.finished)
                    self.assertEqual(self.project.snapshots.get()["id"], before)
                    self.assertFalse((ctx.run_dir / "adapter-settlement.json").exists())
                    self.assertEqual(read_json(ctx.run_dir / "error.json")["status"], "interrupted")

    def test_settlement_error_does_not_hide_final_verification_failure(self):
        before = self.project.snapshots.get()["id"]
        def work(ctx, *_):
            ctx.verification = {"passed": True, "code_digest": ctx.project.snapshots.capture_files(ctx.workspace)["code_digest"]}
            ctx.handle("finish", {**FINISH, "outcome": "done"})
            (ctx.workspace / "app.py").write_bytes(b"VALUE = 9\n")
            raise ProtocolError("settlement failed")
        driver = ScriptDriver(work)
        with self.assertRaisesRegex(Denied, "Code changed after verification"):
            Engine(self.config, driver=driver).run(self.project, "worker")
        ctx = driver.calls[0][0]
        self.assertEqual(ctx.finished["outcome"], "done")
        self.assertEqual(self.project.snapshots.get()["id"], before)
        error = read_json(ctx.run_dir / "error.json")
        self.assertEqual(error["publication"], "not_published")
        self.assertFalse(error["model"]["settled"])
        self.assertEqual(read_json(self.project.root / "health/worker.json")["consecutive_failures"], 1)

    def test_settlement_error_does_not_hide_final_storage_faults(self):
        for method in ("capture_files", "create", "publish"):
            with self.subTest(method=method):
                before = self.project.snapshots.get()["id"]
                def work(ctx, *_):
                    ctx.handle("finish", FINISH)
                    raise ProtocolError("settlement failed")
                driver = ScriptDriver(work)
                fault = OSError(errno.EIO, "injected " + method)
                with patch.object(type(self.project.snapshots), method, side_effect=fault):
                    with self.assertRaises(OSError) as raised:
                        Engine(self.config, driver=driver).run(self.project, "worker")
                self.assertIs(raised.exception, fault)
                ctx = driver.calls[0][0]
                self.assertIsNotNone(ctx.finished)
                self.assertEqual(self.project.snapshots.get()["id"], before)
                error = read_json(ctx.run_dir / "error.json")
                self.assertEqual(error["status"], "interrupted")
                self.assertEqual(error["publication"], "not_published")
                self.assertFalse(error["model"]["settled"])

    def test_unsealed_driver_error_still_fails_and_brakes(self):
        config = dataclasses.replace(self.config, limits=dataclasses.replace(self.config.limits, max_failures=1))
        def work(ctx, *_):
            raise ProtocolError("unsealed transport fault")
        driver = ScriptDriver(work)
        with self.assertRaises(ProtocolError):
            Engine(config, driver=driver).run(self.project, "worker")
        ctx = driver.calls[0][0]
        self.assertIsNone(ctx.finished)
        self.assertFalse((ctx.run_dir / "adapter-settlement.json").exists())
        self.assertEqual(read_json(self.project.root / "health/worker.json")["consecutive_failures"], 1)
        self.assertTrue(self.project.control()["paused"])

    def test_child_seals_keep_answers_without_claiming_adapter_settlement(self):
        roles = dict(self.config.roles)
        roles["searcher"] = dataclasses.replace(roles["searcher"], capabilities=("files", "read", "finish"))
        config = dataclasses.replace(self.config, roles=roles)
        before = self.project.snapshots.get()["id"]
        parent = self.context()
        def work(ctx, *_):
            ctx.handle("finish", FINISH)
            ctx.handle("_budget", {"sequence": 2})
        driver = ScriptDriver(work)
        engine = Engine(config, driver=driver)
        consulted = engine.consult(parent, {"role": "consult", "question": "review"})["answers"][0]
        delegated = engine.delegate(parent, {"role": "searcher", "task": "inspect"})
        for record, filename in ((consulted, "consultation.json"), (delegated, "delegation.json")):
            self.assertEqual(record["answer"], FINISH["summary"])
            stored = read_json(self.project.root / "runs" / record["run"] / filename)
            self.assertEqual(stored["status"], "completed")
            self.assertFalse(stored["model"]["settled"])
        self.assertIsNone(parent.admission_error)
        self.assertEqual(self.project.snapshots.get()["id"], before)  # Children publish nothing.

    def test_finish_or_diagnostic_persistence_failure_remains_blocking(self):
        for filename in ("finish-intent.json", "adapter-settlement.json"):
            with self.subTest(filename=filename):
                before = self.project.snapshots.get()["id"]
                def work(ctx, *_):
                    ctx.handle("finish", FINISH)
                    raise ProtocolError("settlement failed")
                fault = OSError(errno.EIO, "injected record persistence fault")
                def failing_write(path, value):
                    if path.name == filename:
                        raise fault
                    write_json(path, value)
                driver = ScriptDriver(work)
                with patch("mizu.runtime.write_json", side_effect=failing_write):
                    with self.assertRaises(OSError) as raised:
                        Engine(self.config, driver=driver).run(self.project, "worker")
                self.assertIs(raised.exception, fault)
                ctx = driver.calls[0][0]
                self.assertEqual(ctx.finished is None, filename == "finish-intent.json")
                self.assertEqual(self.project.snapshots.get()["id"], before)
                self.assertEqual(read_json(ctx.run_dir / "error.json")["status"], "interrupted")

    def test_settlement_error_does_not_hide_mode_links_or_special_files(self):
        variants = ["symlink", "hardlink"]
        if os.name != "nt":
            variants.append("mode")
        if hasattr(os, "mkfifo"):
            variants.append("fifo")
        for variant in variants:
            with self.subTest(variant=variant):
                self.project.set_control(armed=True, paused=False)
                before = self.project.snapshots.get()["id"]
                path = self.project.workspace / "app.py"
                alias = self.project.workspace / "alias"
                mode = path.stat().st_mode
                def work(ctx, *_):
                    ctx.verification = {"passed": True, "code_digest": ctx.project.snapshots.capture_files(ctx.workspace)["code_digest"]}
                    ctx.handle("finish", {**FINISH, "outcome": "done"})
                    if variant == "mode":
                        path.chmod(0o755)
                    elif variant == "symlink":
                        alias.symlink_to("app.py")
                    elif variant == "hardlink":
                        os.link(path, alias)
                    else:
                        os.mkfifo(alias)
                    raise ProtocolError("settlement failed")
                try:
                    with self.assertRaisesRegex(Denied, "Code changed after verification"):
                        Engine(self.config, driver=ScriptDriver(work)).run(self.project, "worker")
                    self.assertEqual(self.project.snapshots.get()["id"], before)
                finally:
                    alias.unlink(missing_ok=True)
                    path.chmod(mode)


class FinishSelectionTests(SelectionFixture):
    def test_sealed_model_failure_never_falls_back_or_records_selection_failure(self):
        role, _ = self.setup_selection()
        self.config = dataclasses.replace(self.config, limits=dataclasses.replace(self.config.limits, max_failures=1))
        def work(ctx, *_):
            ctx.model_evidence = {"engine": "pi-durable", "requests": ctx.request_count, "usage_known": False, "settled": True}
            ctx.handle("finish", FINISH)
            raise ModelFailure("pi-durable", kind="unanswered", code="synthetic-exhausted", message="x" * 5000)
        driver = ScriptDriver(work)
        recorded = []
        original = State.result
        def recording(store, decision, **kwargs):
            recorded.append(kwargs)
            return original(store, decision, **kwargs)
        with patch.object(State, "result", recording):
            result = Engine(self.config, driver=driver).run(self.project, role.name)
        ctx = driver.calls[0][0]
        self.assertEqual(len(driver.calls), 1)
        self.assertEqual(ctx.attempt_ordinal, 0)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["finish"], read_json(ctx.run_dir / "finish-intent.json"))
        self.assertEqual(result["selection"]["profile"], "primary")
        self.assertEqual(len(recorded), 1)
        self.assertNotIn("error", recorded[0])  # Ordinary work completion only, not on_error/fallback.
        state = State(self.config).read()
        self.assertEqual(state["blocks"], {})
        self.assertEqual(state["contributions"], {})
        self.assertEqual(state["receipts"], {})
        self.assertFalse(result["model"]["settled"])
        self.assertEqual(result["model"]["settlement"]["error_type"], "ModelFailure")
        self.assertLessEqual(len(result["model"]["settlement"]["error"]), 4000)
        self.assertNotIn("durable_result", result["model"])
        self.assertNotIn("session_id", result["model"])
        self.assertEqual(read_json(self.project.root / "health/worker.json")["consecutive_failures"], 0)
        self.assertFalse(self.project.control()["paused"])
