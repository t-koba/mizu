"""Offline wall/request clocks and admitted joins; no live provider claims."""
import dataclasses
import errno
import queue
import sys
import threading
import time
from unittest.mock import patch

from support import Fixture, ScriptDriver, ROOT
from mizu.bridge import Bridge
from mizu.claude import ClaudeDriver
from mizu.codex import CodexDriver
from mizu.config import load, role_run_seconds
from mizu.drivers import admit_invocation
from mizu.engine_channel import Channel
from mizu.errors import Cancelled, ConfigError, DeadlineWrapUp, Denied, LimitExceeded, RunDeadlineExceeded, ModelFailure, ProtocolError
from mizu.fs import read_json, write_json
from mizu.mcp_proxy import forward
from mizu.pi_durable import PiDurableDriver, interpret_result
from mizu.process import environment, run
from mizu.protocol import tool_definitions
from mizu.runtime import Engine, WRAP_UP_REQUESTS, WRAP_UP_TOOLS

FINISH = {"outcome": "wait", "summary": "Saved result", "state": "Next: review."}


class UnsealedDriver(ScriptDriver):
    """Synthetic normal transport return without ScriptDriver's auto-finish."""
    def execute(self, context, prompt, *, profile=None):
        self.calls.append((context, prompt, profile))
        context.handle("_hello", {})
        context.handle("_budget", {"sequence": 1})
        self.callback(context, prompt, profile)
        if not context.host_cancelled() and not context.operations_stopped.is_set():
            for attempt, sequence in tuple(context.request_deadlines):
                if attempt == context.execution_attempt:
                    context.handle("_request_done", {"sequence": sequence})
        return {"requests": context.request_count}


class DeadlineTests(Fixture):
    def configure_wall(self, seconds, *, request=1200, role="worker"):
        self.config = dataclasses.replace(self.config,
            limits=dataclasses.replace(self.config.limits, model_request_seconds=request),
            roles={**self.config.roles, role: dataclasses.replace(self.config.roles[role], run_seconds=seconds)})

    def test_defaults_inheritance_zero_and_positive(self):
        self.assertEqual(self.config.limits.run_seconds, 1200)
        self.assertEqual(self.config.limits.model_request_seconds, 1200)
        self.assertIsNone(self.config.roles["worker"].run_seconds)
        for value in (None, 0, 3600, 1073741824):
            with self.subTest(value=value):
                text = self.file.read_text()
                if value is not None:
                    text = text.replace("[roles.worker]", f"[roles.worker]\nrun_seconds = {value}")
                self.file.write_text(text)
                config = load(self.file)
                self.config = config
                with patch("mizu.runtime.time.monotonic", return_value=1000):
                    ctx = self.context()
                self.assertEqual(role_run_seconds(config, ctx.role), 1200 if value is None else value)
                self.assertEqual(ctx.deadline, None if value == 0 else 1000 + (1200 if value is None else value))
                # Reset the source, not the resolved run.
                self.file.write_text(text.replace(f"\nrun_seconds = {value}", "", 1) if value is not None else text)

    def test_reject_invalid_wall_and_request_policies(self):
        original = self.file.read_text()
        for value in ("true", "false", "-1", '"1200"', "1.5", "1073741825"):
            with self.subTest(role=value), self.assertRaises(ConfigError):
                self.file.write_text(original.replace("[roles.worker]", f"[roles.worker]\nrun_seconds = {value}"))
                load(self.file)
        for key in ("run_seconds", "model_request_seconds"):
            for value in ("0", "-1", "true", '"12"', "1.5", "1073741825"):
                text = original
                if key == "run_seconds":
                    text = text.replace("run_seconds = 1200", f"run_seconds = {value}")
                else:
                    text = text.replace("model_request_seconds = 1200",
                                        f"model_request_seconds = {value}", 1)
                with self.subTest(limit=key, value=value), self.assertRaises(ConfigError):
                    self.file.write_text(text)
                    load(self.file)
        self.file.write_text(original.replace("model_request_seconds = 1200",
                                              "model_request_seconds = 90", 1))
        self.assertEqual(load(self.file).limits.model_request_seconds, 90)

    def test_multiple_requests_cross_former_wall_without_renewing_each_request(self):
        self.configure_wall(0, request=900)
        with patch("mizu.runtime.time.monotonic", return_value=1000) as clock:
            ctx = self.context()
            for seq in (1, 2, 3):
                start = 1000 + (seq - 1) * 801
                clock.return_value = start
                ctx.handle("_budget", {"sequence": seq})
                expiry = ctx.request_deadlines[(0, seq)]
                clock.return_value = start + 800
                ctx.check_deadlines()
                ctx.handle("_request_done", {"sequence": seq})  # No usage required.
                self.assertEqual(expiry, start + 900)
            self.assertGreater(clock.return_value, 1000 + self.config.limits.run_seconds)
            ctx.handle("finish", FINISH)
            self.assertEqual(ctx.finished["outcome"], "wait")
            self.assertIsNone(ctx.deadline)
            self.assertEqual(ctx.request_count, 3)
            self.assertIsNone(ctx.bound_hit)

    def test_progress_duplicate_admission_and_done_cannot_renew_request_or_idle_deadline(self):
        self.configure_wall(0, request=10)
        with patch("mizu.runtime.time.monotonic", return_value=1000) as clock:
            ctx = self.context()
            ctx.handle("_budget", {"sequence": 1})
            channel = Channel.__new__(Channel)
            channel.context, channel.records = ctx, queue.Queue()
            for instant in (1001, 1004, 1009):
                clock.return_value = instant
                ctx.handle("_budget", {"sequence": 1})
                ctx.handle("_hello", {})
                ctx.handle("_model_usage", {"sequence": 2, "usage": {}})
                channel.records.put({"type": "heartbeat"})
                channel.receive()
                self.assertEqual(ctx.request_deadlines[(0, 1)], 1010)
            clock.return_value = 1010
            with self.assertRaisesRegex(LimitExceeded, "Logical model request"):
                channel.receive()
        with patch("mizu.runtime.time.monotonic", return_value=2000) as clock:
            ctx = self.context()
            ctx.handle("_budget", {"sequence": 1})
            clock.return_value = 2001
            ctx.handle("_request_done", {"sequence": 1})
            clock.return_value = 2009
            ctx.handle("_request_done", {"sequence": 1})
            self.assertEqual(ctx.transport_idle_deadline, 2011)
            clock.return_value = 2011
            with self.assertRaisesRegex(LimitExceeded, "stalled"):
                ctx.check_deadlines()

    def test_enclosing_query_and_turn_admission_is_finite_and_paid_per_attempt(self):
        for unit in ("query", "turn"):
            self.configure_wall(0, request=10)
            with self.subTest(unit=unit), patch("mizu.runtime.time.monotonic", return_value=1000) as clock:
                ctx = self.context()
                with patch("mizu.drivers.Budget.take") as take:
                    admit_invocation(ctx, unit=unit)
                    admit_invocation(ctx, unit=unit)
                    self.assertEqual(take.call_count, 1)
                    self.assertEqual(ctx.request_deadlines[(0, 0)], 1010)
                    clock.return_value = 1010
                    with self.assertRaisesRegex(LimitExceeded, "Logical model request"):
                        admit_invocation(ctx, unit=unit)
                    ctx.request_deadlines.clear()  # Reaped prior invocation.
                    ctx.execution_attempt = 1
                    ctx.transport_idle_deadline = 1020
                    admit_invocation(ctx, unit=unit)
                    self.assertEqual(take.call_count, 2)
                    self.assertNotEqual(take.call_args_list[0].args[0], take.call_args_list[1].args[0])
                    self.assertEqual(ctx.request_deadlines[(1, 0)], 1020)

    def test_hung_and_trickling_channel_are_host_bounded_even_without_wall(self):
        self.configure_wall(0, request=1)
        for code in ("import time; time.sleep(60)",
                     "import time;\nwhile True:\n print('{\"type\":\"heartbeat\"}', flush=True); time.sleep(.01)"):
            with self.subTest(peer=code):
                ctx = self.context()
                ctx.handle("_budget", {"sequence": 1})
                channel = Channel(ctx, [sys.executable, "-c", code], environment(), ctx.workspace, "fake")
                started = time.monotonic()
                try:
                    with self.assertRaisesRegex(LimitExceeded, "Logical model request"):
                        while True:
                            channel.receive()
                    self.assertLess(time.monotonic() - started, 3)
                finally:
                    channel.close()
                self.assertIsNotNone(channel.process.poll())

    def test_stalled_durable_process_uses_host_request_not_global_wall(self):
        self.configure_wall(0, request=1)
        ctx = self.context()
        ctx.handle("_budget", {"sequence": 1})
        started = time.monotonic()
        with self.assertRaisesRegex(LimitExceeded, "Logical model request"):
            run([sys.executable, "-c", "import time; time.sleep(60)"],
                timeout=ctx.transport_deadline, maximum=1000, cancel=ctx.host_cancelled)
        self.assertLess(time.monotonic() - started, 3)

    def test_durable_driver_multiple_requests_have_no_launcher_wide_timer(self):
        self.configure_wall(0, request=900)
        ctx = self.context()
        driver = PiDurableDriver(self.config)
        settings = {"options": {}, "model": "test-model", "provider": "test-provider", "session": "ephemeral"}
        model = {"provider": "test-provider", "model": "test-model"}
        def fake_run(*_, timeout, **kwargs):
            self.assertTrue(callable(timeout))
            start = time.monotonic()
            self.assertGreater(timeout(), start)
            for sequence in (1, 3):
                clock.return_value = start + (sequence - 1) * 400
                ctx.handle("_budget", {"sequence": sequence})
                clock.return_value += 800
                ctx.handle("_request_done", {"sequence": sequence})
            ctx.handle("finish", FINISH)
            return type("Result", (), {"reason": "exited", "exit_code": 0, "stdout": "{}"})()
        with patch("mizu.runtime.time.monotonic", return_value=1000) as clock, \
             patch("mizu.pi_durable.run", side_effect=fake_run), patch("mizu.pi_durable.interpret_result"):
            ctx.transport_idle_deadline = 1900
            driver._run_once(ctx, settings, model, "prompt", "primary", "key", ctx.run_dir / "store.sqlite")
        effective = read_json(ctx.run_dir / "pi-durable-effective.json")
        self.assertNotIn("deadline_ms", effective)
        self.assertGreater(clock.return_value, 2200)

    def test_finite_wall_is_resolved_once_across_transports(self):
        self.configure_wall(100)
        with patch("mizu.runtime.time.monotonic", return_value=1000) as clock:
            ctx = self.context()
            wall = ctx.deadline
            engine = Engine(self.config)
            class Failure:
                def execute(self, context, *_):
                    context.handle("_budget", {"sequence": 1})
                    raise ModelFailure("pi", kind="error", message="offline")
            with self.assertRaises(ModelFailure):
                engine._execute(ctx, Failure(), "attempt")
            ctx.execution_attempt += 1
            ctx.role = dataclasses.replace(ctx.role, profile="other", run_seconds=0)
            clock.return_value = 1040
            with self.assertRaises(ModelFailure):
                engine._execute(ctx, Failure(), "fallback")
            self.assertEqual(ctx.deadline, wall)
            self.assertEqual(ctx.run_seconds, 100)
            self.assertEqual(ctx.request_count, 2)
            clock.return_value = 1100
            with self.assertRaises(RunDeadlineExceeded):
                ctx.handle("_budget", {"sequence": 2})

    def test_early_wall_wrap_up_keeps_allowances_and_seals_bounded(self):
        self.configure_wall(100)
        with patch("mizu.runtime.time.monotonic", return_value=1000) as clock:
            ctx = self.context()
            self.assertEqual(ctx.wrap_up_at, 1075)
            ctx.handle("_budget", {"sequence": 1})
            clock.return_value = 1075
            with self.assertRaises(DeadlineWrapUp):
                ctx.check_deadlines(wrap_up=True)
            self.assertEqual(ctx.bound_hit, "deadline")
            with self.assertRaises(Denied):
                ctx.handle("read", {"path": "app.py"})
            for sequence in range(2, 2 + WRAP_UP_REQUESTS):
                ctx.handle("_budget", {"sequence": sequence})
            with self.assertRaises(LimitExceeded):
                ctx.handle("_budget", {"sequence": 20})
            self.assertEqual(ctx.wrap_up_requests, 3)
            ctx.handle("finish", FINISH)
            self.assertEqual(ctx.wrap_up_tools, 1)
            self.assertEqual(WRAP_UP_TOOLS, 5)
            self.assertEqual(ctx.finished["outcome"], "bounded")
            self.assertEqual(read_json(ctx.run_dir / "finish-intent.json")["bound"], "deadline")

    def test_wrap_up_precedes_later_request_expiry_without_renewing_it(self):
        self.configure_wall(100, request=30)
        with patch("mizu.runtime.time.monotonic", return_value=1000) as clock:
            ctx = self.context()
            self.assertEqual(ctx.wrap_up_at, 1075)
            ctx.handle("_budget", {"sequence": 1})
            clock.return_value = 1025
            ctx.handle("_request_done", {"sequence": 1})
            clock.return_value = 1050
            ctx.handle("_budget", {"sequence": 2})
            deadlines = dict(ctx.request_deadlines)
            self.assertEqual(deadlines, {(0, 2): 1080})
            clock.return_value = 1081
            with self.assertRaises(DeadlineWrapUp):
                ctx.check_deadlines(wrap_up=True)
            self.assertEqual(ctx.bound_hit, "deadline")
            self.assertEqual(ctx.request_deadlines, deadlines)
            ctx.wrap_up_attempted = True
            with self.assertRaisesRegex(LimitExceeded, "Logical model request") as raised:
                ctx.check_deadlines(wrap_up=True)
            self.assertNotIsInstance(raised.exception, RunDeadlineExceeded)
            self.assertEqual(ctx.request_deadlines, deadlines)

    def test_request_expiring_before_wrap_up_remains_a_request_failure(self):
        self.configure_wall(100, request=30)
        with patch("mizu.runtime.time.monotonic", return_value=1000) as clock:
            ctx = self.context()
            ctx.handle("_budget", {"sequence": 1})
            clock.return_value = 1076
            with self.assertRaisesRegex(LimitExceeded, "Logical model request") as raised:
                ctx.check_deadlines(wrap_up=True)
            self.assertNotIsInstance(raised.exception, RunDeadlineExceeded)
            self.assertIsNone(ctx.bound_hit)
            self.assertEqual(ctx.request_deadlines, {(0, 1): 1030})

    def test_unsealed_deadline_wrap_up_return_and_allowance_exhaustion_are_bounded(self):
        self.configure_wall(100)
        self.config = dataclasses.replace(self.config,
            limits=dataclasses.replace(self.config.limits, max_failures=1))
        worker = self.config.roles["worker"]
        self.config = dataclasses.replace(self.config, roles={**self.config.roles,
            "worker": dataclasses.replace(worker, capabilities=(*worker.capabilities, "research"))})
        for ending in ("return", "wall_return", "requests", "tools", "durable_requests"):
            with self.subTest(ending=ending):
                self.project.set_control(armed=True, paused=False)
                before = self.project.snapshots.get()
                health = {"consecutive_failures": 2, "last_run": "prior"}
                write_json(self.project.root / "health/worker.json", health)
                def work(ctx, *_):
                    (ctx.workspace / "app.py").write_text("unsealed wrap-up changes")
                    if len(driver.calls) == 1:
                        ctx.wrap_up_at = time.monotonic() - 1
                        ctx.check_deadlines(wrap_up=True)
                    self.assertTrue(ctx.wrap_up_attempted)
                    self.assertGreater(ctx.deadline, time.monotonic())
                    self.assertEqual(ctx.role.engine_tools, ())
                    self.assertNotIn("exec", ctx.role.capabilities)
                    if ending == "wall_return":
                        ctx.deadline = time.monotonic() - 1
                    elif ending in ("requests", "durable_requests"):
                        for sequence in range(2, WRAP_UP_REQUESTS + 1):
                            ctx.handle("_budget", {"sequence": sequence})
                        if ending == "requests":
                            ctx.handle("_budget", {"sequence": 20})
                        else:
                            ctx.model_evidence = {}
                            # The bridge reports the host refusal to the launcher.
                            with self.assertRaises(LimitExceeded):
                                ctx.handle("_budget", {"sequence": 20})
                            interpret_result({"durable_result": True, "status": "request_bound"},
                                             ctx, {}, None)
                    elif ending == "tools":
                        with patch.object(ctx, "dispatch", return_value={}):
                            for _ in range(WRAP_UP_TOOLS + 1):
                                ctx.handle("research", {"expected_generation": 0, "state": "{}"})
                driver = UnsealedDriver(work)
                with patch.object(type(self.project.snapshots), "create") as create:
                    result = Engine(self.config, driver=driver).run(self.project, "worker")
                create.assert_not_called()
                ctx = driver.calls[-1][0]
                self.assertEqual(len(driver.calls), 2)
                self.assertIs(driver.calls[0][0], ctx)
                self.assertEqual(result["status"], "bounded")
                self.assertEqual(result["bound"], "deadline")
                self.assertEqual(result["publication"], "not_published")
                self.assertNotIn("finish", result)
                self.assertEqual(read_json(ctx.run_dir / "result.json"), result)
                self.assertFalse((ctx.run_dir / "finish-intent.json").exists())
                self.assertFalse((ctx.run_dir / "error.json").exists())
                self.assertEqual(read_json(self.project.root / "health/worker.json"), health)
                self.assertEqual(self.project.snapshots.get()["id"], before["id"])
                self.assertFalse(self.project.control()["paused"])

    def test_deadline_native_request_allowance_exhaustion_is_explicit(self):
        self.configure_wall(100)
        for unit in ("query", "turn"):
            with self.subTest(unit=unit), patch("mizu.runtime.time.monotonic", return_value=1000) as clock:
                ctx = self.context()
                clock.return_value = 1075
                with self.assertRaises(DeadlineWrapUp):
                    ctx.check_deadlines(wrap_up=True)
                ctx.wrap_up_attempted = True
                for attempt in range(WRAP_UP_REQUESTS):
                    ctx.execution_attempt = attempt
                    admit_invocation(ctx, unit=unit)
                self.assertEqual(ctx.wrap_up_requests, WRAP_UP_REQUESTS)
                ctx.execution_attempt += 1
                with self.assertRaises(RunDeadlineExceeded):
                    admit_invocation(ctx, unit=unit)

    def test_unrelated_pre_seal_faults_after_wall_remain_failures(self):
        self.configure_wall(100)
        self.config = dataclasses.replace(self.config,
            limits=dataclasses.replace(self.config.limits, max_failures=1))
        for wrap_up in (False, True):
            for fault in (ProtocolError("synthetic transport defect"),
                          OSError(errno.EIO, "synthetic storage defect"),
                          ConfigError("synthetic configuration defect"),
                          ModelFailure("pi", kind="error", message="synthetic provider defect"),
                          LimitExceeded("synthetic non-deadline limit"),
                          Cancelled("synthetic non-deadline cancellation")):
                with self.subTest(wrap_up=wrap_up, fault=type(fault).__name__):
                    self.project.set_control(armed=True, paused=False)
                    write_json(self.project.root / "health/worker.json", {"consecutive_failures": 0})
                    before = self.project.snapshots.get()["id"]
                    def work(ctx, *_):
                        if wrap_up and len(driver.calls) == 1:
                            ctx.wrap_up_at = time.monotonic() - 1
                            ctx.check_deadlines(wrap_up=True)
                        ctx.deadline = time.monotonic() - 1
                        raise fault
                    driver = UnsealedDriver(work)
                    with self.assertRaises(type(fault)) as raised:
                        Engine(self.config, driver=driver).run(self.project, "worker")
                    self.assertIs(raised.exception, fault)
                    ctx = driver.calls[-1][0]
                    error = read_json(ctx.run_dir / "error.json")
                    self.assertEqual(error["status"], "interrupted")
                    self.assertEqual(error["publication"], "not_published")
                    self.assertFalse((ctx.run_dir / "result.json").exists())
                    self.assertFalse((ctx.run_dir / "finish-intent.json").exists())
                    self.assertEqual(read_json(self.project.root / "health/worker.json")["consecutive_failures"], 1)
                    self.assertEqual(self.project.control()["paused"], not isinstance(fault, Cancelled))
                    self.assertEqual(self.project.snapshots.get()["id"], before)

    def test_durable_error_results_after_wall_are_not_deadline_successes(self):
        self.configure_wall(100)
        for reason, exit_code, stdout, expected in (
                ("input_error", None, "", ProtocolError),
                ("output_error", None, "", ProtocolError),
                ("output_limit", None, "", ProtocolError),
                ("exited", 1, "synthetic launcher fault", ModelFailure),
                ("exited", 0, "invalid result", ProtocolError)):
            with self.subTest(reason=reason, exit_code=exit_code):
                ctx = self.context()
                driver = PiDurableDriver(self.config)
                settings = {"options": {}, "model": "test-model", "provider": "test-provider", "session": "ephemeral"}
                model = {"provider": "test-provider", "model": "test-model"}
                def fake_run(*_, **kwargs):
                    ctx.deadline = time.monotonic() - 1
                    return type("Result", (), {"reason": reason, "exit_code": exit_code,
                                                "stdout": stdout, "stderr": ""})()
                with patch("mizu.pi_durable.run", side_effect=fake_run), self.assertRaises(expected):
                    driver._run_once(ctx, settings, model, "prompt", "primary", "key", ctx.run_dir / "store.sqlite")
                self.assertIsNone(ctx.finished)

    def test_explicit_host_cancellation_with_expired_wall_is_not_bounded(self):
        for action in ("stop", "pause", "unarm"):
            with self.subTest(action=action):
                self.project.set_control(armed=True, paused=False)
                before = self.project.snapshots.get()["id"]
                def work(ctx, *_):
                    ctx.deadline = time.monotonic() - 1
                    if action == "stop":
                        ctx.stop.set()
                    else:
                        self.project.set_control(**({"paused": True} if action == "pause" else {"armed": False}))
                driver = UnsealedDriver(work)
                with self.assertRaises(Cancelled):
                    Engine(self.config, driver=driver).run(self.project, "worker")
                ctx = driver.calls[0][0]
                self.assertEqual(len(driver.calls), 1)  # Explicit cancellation never starts finish follow-up.
                self.assertEqual(read_json(ctx.run_dir / "error.json")["status"], "interrupted")
                self.assertFalse((ctx.run_dir / "result.json").exists())
                self.assertEqual(self.project.snapshots.get()["id"], before)

    def test_normal_return_without_finish_is_not_bounded_by_unrelated_limits(self):
        for bound in (None, "requests", "tools"):
            with self.subTest(bound=bound):
                self.project.set_control(armed=True, paused=False)
                def work(ctx, *_):
                    ctx.bound_hit = bound
                from mizu.errors import MissingFinish
                with self.assertRaises(MissingFinish):
                    Engine(self.config, driver=UnsealedDriver(work)).run(self.project, "worker")

    def test_send_timeout_classifies_only_deadline_cancellation_or_limit(self):
        self.configure_wall(100)
        for fault in (Cancelled("RPC send cancelled"), LimitExceeded("RPC input deadline exceeded"),
                      ProtocolError("synthetic broken input"), OSError(errno.EIO, "synthetic send fault")):
            with self.subTest(fault=type(fault).__name__), \
                 patch("mizu.runtime.time.monotonic", return_value=1000) as clock:
                ctx = self.context()
                channel = Channel.__new__(Channel)
                channel.context, channel.process = ctx, object()
                def fail_send(*_, **kwargs):
                    clock.return_value = 1100
                    raise fault
                expected = RunDeadlineExceeded if isinstance(fault, (Cancelled, LimitExceeded)) else type(fault)
                with patch("mizu.engine_channel.send_bounded", side_effect=fail_send), \
                     self.assertRaises(expected) as raised:
                    channel.send({"method": "synthetic"})
                if expected is not RunDeadlineExceeded:
                    self.assertIs(raised.exception, fault)

    def test_wall_wrap_restarts_only_transport_and_preserves_final_seal(self):
        self.configure_wall(100)
        calls = []
        def work(ctx, *_):
            calls.append(ctx)
            if len(calls) == 1:
                ctx.wrap_up_at = time.monotonic() - 1
                ctx.check_deadlines(wrap_up=True)
            self.assertEqual(ctx.role.engine_tools, ())
            self.assertNotIn("exec", ctx.role.capabilities)
            ctx.handle("finish", FINISH)
            ctx.deadline = time.monotonic() - 1
            raise LimitExceeded("settlement timeout after seal")
        result = Engine(self.config, driver=ScriptDriver(work)).run(self.project, "worker")
        self.assertEqual(len(calls), 2)
        self.assertIs(calls[0], calls[1])
        self.assertEqual(result["finish"]["outcome"], "bounded")
        self.assertEqual(result["status"], "completed")
        self.assertFalse(self.project.control()["paused"])

    def test_wall_exhaustion_without_finish_never_publishes_or_counts_failure(self):
        before = self.project.snapshots.get()
        health = {"consecutive_failures": 2, "last_run": "prior"}
        write_json(self.project.root / "health/worker.json", health)
        def work(ctx, *_):
            (ctx.workspace / "app.py").write_text("unsealed changes")
            ctx.deadline = time.monotonic() - 1
            ctx.handle("finish", FINISH)
        result = Engine(self.config, driver=ScriptDriver(work)).run(self.project, "worker")
        self.assertEqual(result["status"], "bounded")
        self.assertEqual(result["publication"], "not_published")
        self.assertNotIn("finish", result)
        self.assertEqual(read_json(self.project.root / "health/worker.json"), health)
        self.assertEqual(self.project.snapshots.get()["id"], before["id"])
        run_dir = self.project.root / "runs" / result["run"]
        self.assertEqual(read_json(run_dir / "result.json")["bound"], "deadline")
        self.assertFalse((run_dir / "finish-intent.json").exists())
        self.assertFalse((run_dir / "error.json").exists())

    def test_bounded_result_storage_fault_remains_a_failure(self):
        self.configure_wall(100)
        before = self.project.snapshots.get()["id"]
        write_json(self.project.root / "health/worker.json", {"consecutive_failures": 0})
        def work(ctx, *_):
            ctx.deadline = time.monotonic() - 1
        driver = UnsealedDriver(work)
        original = write_json
        def fail_bounded_result(path, value, *args, **kwargs):
            if str(path).endswith("/result.json") and isinstance(value, dict) and value.get("bound") == "deadline":
                raise OSError(errno.EIO, "synthetic bounded-record storage fault")
            return original(path, value, *args, **kwargs)
        with patch("mizu.runtime.write_json", side_effect=fail_bounded_result), \
             self.assertRaisesRegex(OSError, "bounded-record storage fault"):
            Engine(self.config, driver=driver).run(self.project, "worker")
        ctx = driver.calls[0][0]
        self.assertEqual(read_json(ctx.run_dir / "error.json")["status"], "interrupted")
        self.assertEqual(read_json(self.project.root / "health/worker.json")["consecutive_failures"], 1)
        self.assertEqual(self.project.snapshots.get()["id"], before)
        self.assertFalse((ctx.run_dir / "finish-intent.json").exists())

    def test_pre_seal_provider_error_is_still_a_failure(self):
        def work(ctx, *_):
            ctx.deadline = time.monotonic() - 1
            raise ModelFailure("pi", kind="error", message="offline provider error")
        with self.assertRaises(ModelFailure):
            Engine(self.config, driver=ScriptDriver(work)).run(self.project, "worker")
        self.assertEqual(read_json(self.project.root / "health/worker.json")["consecutive_failures"], 1)

    def joined_config(self, parent_seconds, child_seconds):
        roles = dict(self.config.roles)
        roles["worker"] = dataclasses.replace(roles["worker"], run_seconds=parent_seconds,
            capabilities=tuple([*roles["worker"].capabilities, "delegate", "consult"]))
        roles["helper"] = dataclasses.replace(roles["searcher"], name="helper", run_seconds=child_seconds,
            capabilities=("read", "finish"))
        roles["consult"] = dataclasses.replace(roles["consult"], run_seconds=child_seconds)
        return dataclasses.replace(self.config, roles=roles)

    def test_parent_child_walls_are_independent_for_all_finite_unlimited_combinations(self):
        for operation in ("delegate", "consult"):
            for parent_seconds, child_seconds in ((100, 200), (100, 0), (0, 200), (0, 0)):
                with self.subTest(operation=operation, parent=parent_seconds, child=child_seconds):
                    config = self.joined_config(parent_seconds, child_seconds)
                    seen = {}
                    def work(ctx, *_):
                        if ctx.role.name == "worker":
                            seen["parent"] = ctx
                            args = ({"role": "helper", "task": "Independent deadline"} if operation == "delegate"
                                    else {"role": "consult", "question": "Independent deadline?"})
                            response = ctx.handle(operation, args)
                            self.assertTrue(response)
                            ctx.handle("finish", FINISH)
                        else:
                            parent = seen["parent"]
                            self.assertFalse(parent.joins_idle.is_set())
                            self.assertEqual(ctx.run_seconds, child_seconds)
                            if child_seconds:
                                self.assertGreater(ctx.deadline, time.monotonic() + child_seconds - 5)
                                if parent_seconds:
                                    self.assertGreater(ctx.deadline, parent.deadline)
                            else:
                                self.assertIsNone(ctx.deadline)
                            self.assertGreater(min(ctx.request_deadlines.values()), time.monotonic())
                            ctx.handle("finish", FINISH)
                    result = Engine(config, driver=ScriptDriver(work)).run(self.project, "worker")
                    self.assertEqual(result["status"], "completed")
                    self.assertEqual(result["finish"]["outcome"], "wait")

    def test_parent_expiry_during_join_never_cancels_child_or_renews_parent(self):
        for operation in ("delegate", "consult"):
            for child_seconds in (0, 200):
                with self.subTest(operation=operation, child=child_seconds):
                    config = self.joined_config(100, child_seconds)
                    seen = {}
                    before = self.project.snapshots.get()["id"]
                    def work(ctx, *_):
                        if ctx.role.name == "worker":
                            seen["parent"] = ctx
                            args = ({"role": "helper", "task": "Finish after parent expiry"} if operation == "delegate"
                                    else {"role": "consult", "question": "Finish after parent expiry?"})
                            answer = ctx.handle(operation, args)
                            if operation == "delegate":
                                self.assertEqual(answer["answer"], FINISH["summary"])
                            else:
                                self.assertEqual(answer["answers"][0]["answer"], FINISH["summary"])
                            with self.assertRaises(RunDeadlineExceeded):
                                ctx.handle("_budget", {"sequence": 20})
                            ctx.check_deadlines()  # No inference allowance after join.
                        else:
                            parent = seen["parent"]
                            parent.deadline = time.monotonic() - 1
                            seen["request_expiry"] = dict(parent.request_deadlines)
                            parent.check_deadlines(wrap_up=True)
                            self.assertFalse(parent.cancelled())
                            self.assertIsNone(parent.transport_deadline())
                            self.assertFalse(ctx.cancelled())
                            ctx.handle("finish", FINISH)
                            seen["child_finished"] = ctx.finished
                    result = Engine(config, driver=ScriptDriver(work)).run(self.project, "worker")
                    self.assertEqual(result["status"], "bounded")
                    self.assertEqual(seen["child_finished"]["summary"], FINISH["summary"])
                    self.assertEqual(seen["parent"].request_deadlines, seen["request_expiry"])
                    self.assertEqual(self.project.snapshots.get()["id"], before)

    def test_child_local_expiry_does_not_expire_unlimited_parent(self):
        config = self.joined_config(0, 100)
        def work(ctx, *_):
            if ctx.role.name == "worker":
                answer = ctx.handle("delegate", {"role": "helper", "task": "Bound this child"})
                self.assertIn("deadline", answer["error"])
                self.assertIsNone(ctx.deadline)
                self.assertIsNone(ctx.bound_hit)
                ctx.handle("finish", FINISH)
            else:
                ctx.deadline = time.monotonic() - 1
                ctx.handle("finish", FINISH)
        result = Engine(config, driver=ScriptDriver(work)).run(self.project, "worker")
        self.assertEqual(result["finish"]["outcome"], "wait")
        child = next(self.project.root.glob("runs/*/error.json"))
        self.assertEqual(read_json(child)["status"], "bounded")

    def test_explicit_stop_pause_and_unarm_still_cancel_joined_children(self):
        for action in ("stop", "pause", "unarm"):
            with self.subTest(action=action):
                self.project.set_control(armed=True, paused=False)
                config = self.joined_config(0, 0)
                seen = {}
                def work(ctx, *_):
                    if ctx.role.name == "worker":
                        seen["parent"] = ctx
                        response = ctx.handle("delegate", {"role": "helper", "task": "Observe cancellation"})
                        self.assertIn("error", response)
                        ctx.handle("finish", FINISH)
                    else:
                        if action == "stop":
                            ctx.stop.set()
                        elif action == "pause":
                            self.project.set_control(paused=True)
                        else:
                            self.project.set_control(armed=False)
                        self.assertTrue(ctx.host_cancelled())
                        self.assertTrue(seen["parent"].host_cancelled())
                        ctx.handle("finish", FINISH)
                with self.assertRaises(Cancelled):
                    Engine(config, driver=ScriptDriver(work)).run(self.project, "worker")

    def test_parent_transport_cleanup_waits_for_admitted_join(self):
        self.configure_wall(0)
        parent = self.context()
        child_admitted, release_child = threading.Event(), threading.Event()
        def child(*_):
            child_admitted.set()
            release_child.wait(3)
            return {"answer": "joined"}
        parent.delegate = child
        result = []
        joined = threading.Thread(target=lambda: result.append(parent.handle("delegate", {"task": "Join"})))
        parent.role = dataclasses.replace(parent.role, capabilities=(*parent.role.capabilities, "delegate"))
        joined.start()
        self.assertTrue(child_admitted.wait(2))
        cleanup = threading.Thread(target=parent.cancel_operations)
        cleanup.start()
        try:
            time.sleep(.05)
            self.assertTrue(cleanup.is_alive())
            self.assertFalse(parent.operations_stopped.is_set())
        finally:
            release_child.set()
            joined.join(3)
            cleanup.join(3)
        self.assertFalse(joined.is_alive())
        self.assertFalse(cleanup.is_alive())
        self.assertEqual(result, [{"answer": "joined"}])
        self.assertTrue(parent.operations_stopped.is_set())

    def test_query_expiry_during_join_is_not_renewed_after_child_returns(self):
        self.configure_wall(0, request=10)
        with patch("mizu.runtime.time.monotonic", return_value=1000) as clock:
            parent = self.context()
            parent.role = dataclasses.replace(parent.role, capabilities=(*parent.role.capabilities, "delegate"))
            admit_invocation(parent, unit="query")
            def child(*_):
                clock.return_value = 1011
                parent.check_deadlines()  # Must drain, not kill this admitted child.
                return {"answer": "completed"}
            parent.delegate = child
            parent.handle("delegate", {"task": "Finish child"})
            self.assertEqual(parent.request_deadlines[(0, 0)], 1010)
            with self.assertRaisesRegex(LimitExceeded, "Logical model request"):
                parent.check_deadlines()

    def test_proxy_join_response_is_not_cut_short_by_caller_rpc_timeout(self):
        def respond(*_):
            time.sleep(.15)
            return {"answer": "child completed"}
        with Bridge(respond, tool_definitions(("delegate",)), timeout=1, transport="tcp") as bridge:
            config = read_json(bridge.config_file)
            self.assertEqual(config["joined_operations"], ["delegate"])
            config["timeout_ms"] = 50
            self.assertEqual(forward(config, "delegate", {}), {"answer": "child completed"})


    def test_pi_tool_keeps_own_bounds_not_between_request_stall_timer(self):
        self.configure_wall(0, request=10)
        with patch("mizu.runtime.time.monotonic", return_value=1000) as clock:
            ctx = self.context()
            ctx.handle("_budget", {"sequence": 1})
            ctx.handle("_request_done", {"sequence": 1})
            def work(*_):
                clock.return_value = 1011
                ctx.check_deadlines()
                self.assertIsNone(ctx.transport_deadline())
                self.assertFalse(ctx.tools_cancelled())
                return {"files": []}
            with patch.object(ctx, "dispatch", side_effect=work):
                ctx.handle("files", {})
            self.assertEqual(ctx.transport_idle_deadline, 1021)
            self.assertFalse(ctx.operation_active.is_set())


    def test_native_query_and_turn_progress_cannot_renew_absolute_deadline(self):
        for engine in ("claude", "codex"):
            with self.subTest(engine=engine):
                self.configure_wall(0, request=1, role="consult")
                profile = self.config.roles["consult"].profile
                config = dataclasses.replace(self.config,
                    profiles={**self.config.profiles, profile: {**self.config.profiles[profile],
                        "engine": engine, "provider": "synthetic", "options": {}, "session": "ephemeral"}},
                    engines={**self.config.engines, engine: {**self.config.engines[engine],
                        "command": (sys.executable, str(ROOT / "tests/fake_engines.py"), engine, "stalled-request")}})
                self.config = config
                ctx = self.context("consult")
                driver = ClaudeDriver(config) if engine == "claude" else CodexDriver(config)
                started = time.monotonic()
                with patch.dict("os.environ", {"CODEX_AUTH_FILE": str(self.root / "absent")}), \
                     self.assertRaisesRegex(LimitExceeded, "Logical model request"):
                    driver.execute(ctx, "Synthetic stalled inference")
                self.assertIsNone(ctx.deadline)
                self.assertEqual(ctx.request_count, 1)
                self.assertIsNone(ctx.finished)
                self.assertLess(time.monotonic() - started, 5)
