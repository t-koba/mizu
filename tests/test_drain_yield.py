"""Offline drain boundaries and fake adapter handshakes; no live/isolation claims."""
import dataclasses
import errno
import threading
from unittest.mock import patch

from support import Fixture
from test_deadlines import UnsealedDriver
from mizu.admission import _Queue
from mizu.budget import Budget
from mizu.config import load
from mizu.drivers import admit_invocation
from mizu.engine_config import effective, save_session, session_record
from mizu.errors import (Busy, Cancelled, ConfigError, DeadlineWrapUp, Denied,
                         DrainYieldExceeded, InfraExceeded, LimitExceeded, ModelFailure,
                         ProtocolError, RunDeadlineExceeded)
from mizu.fs import lock, read_json, write_json
from mizu.pi import PiDriver
from mizu.pi_durable import PiDurableDriver, interpret_result
from mizu.pi_durable_store import load_run, open_store
from mizu.runtime import Engine, WRAP_UP_REQUESTS, WRAP_UP_TOOLS

FINISH = {"outcome": "continue", "summary": "Saved work for drain", "state": "Next: resume."}


class DrainYieldTests(Fixture):
    def test_default_and_strict_boolean_policy(self):
        self.assertTrue(self.config.limits.drain_yield)
        original = self.file.read_text()
        for value in ("true", "false", "0", "1", '"true"', "1.0", "[]"):
            text = original.replace("drain_yield = true", f"drain_yield = {value}")
            self.file.write_text(text)
            with self.subTest(value=value):
                if value in ("true", "false"):
                    self.assertEqual(load(self.file).limits.drain_yield, value == "true")
                else:
                    with self.assertRaises(ConfigError):
                        load(self.file)
        self.file.write_text(original.replace("drain_yield = true", "# omitted drain policy"))
        self.assertTrue(load(self.file).limits.drain_yield)

    def test_drain_waits_for_request_done_without_renewing_request(self):
        ctx = self.context()
        ctx.handle("_budget", {"sequence": 1})
        deadlines = dict(ctx.request_deadlines)
        self.project.set_control(draining=True)
        ctx.check_deadlines(wrap_up=True)
        self.assertIsNone(ctx.bound_hit)
        self.assertEqual(ctx.request_deadlines, deadlines)
        self.assertEqual(ctx.wrap_up_requests, 0)
        ctx.handle("_request_done", {"sequence": 1})
        with self.assertRaises(DeadlineWrapUp):
            ctx.check_deadlines(wrap_up=True)
        self.assertEqual(ctx.bound_hit, "drain")
        self.assertFalse(ctx.request_deadlines)

    def test_failed_request_completion_is_not_a_drain_boundary(self):
        ctx = self.context()
        ctx.handle("_budget", {"sequence": 1})
        self.project.set_control(draining=True)
        ctx.handle("_request_done", {"sequence": 1, "failed": True})
        ctx.check_deadlines(wrap_up=True)
        self.assertIsNone(ctx.bound_hit)
        self.assertTrue(ctx.request_failure_pending)

    def test_non_pi_invocation_yields_before_budget_admission(self):
        ctx = self.context()
        budget = Budget(self.config.data / "budget", self.config.limits.daily_requests,
                        self.config.limits.retention_days,
                        self.config.limits.shared_daily_requests, self.config.timezone)
        before = budget.usage(self.project.name)["used"]
        self.project.set_control(draining=True)
        with self.assertRaises(DeadlineWrapUp):
            admit_invocation(ctx, unit="query")
        self.assertEqual(ctx.bound_hit, "drain")
        self.assertEqual(ctx.request_count, 0)
        self.assertFalse(ctx.request_deadlines)
        self.assertEqual(budget.usage(self.project.name)["used"], before)

    def test_pi_drain_transition_reuses_persistent_session(self):
        for boundary in ("budget", "transport_poll", "provider_error"):
            with self.subTest(boundary=boundary):
                ctx = self.context()
                profile = ctx.role.profile
                settings = effective(self.config, ctx.role, profile)
                path, _ = session_record(ctx, profile, settings)
                path.unlink(missing_ok=True)
                self.project.set_control(draining=True)

                class BridgeStub:
                    def __init__(self, handler, *args, **kwargs):
                        self.config_file = ctx.run_dir / "bridge.json"
                    def __enter__(self):
                        return self
                    def __exit__(self, *args):
                        return False

                class ChannelStub:
                    def __init__(self, context, *args, **kwargs):
                        self.context = context
                        self.received = 0
                    def send(self, event):
                        return None
                    def receive(self, until=None):
                        self.received += 1
                        if self.received == 1:
                            self.context.handle("_hello", {})
                            model = self.config.model(profile)
                            return {"id": "state", "success": True,
                                    "data": {"model": {"id": model["model"], "provider": model["provider"]},
                                             "sessionFile": "persistent-drain-session"}}
                        if self.boundary == "transport_poll":
                            self.context.check_deadlines(wrap_up=True)
                            raise AssertionError("drain transport poll should yield")
                        if self.boundary == "provider_error":
                            self.context.project.set_control(draining=False)
                            self.context.handle("_budget", {"sequence": 1})
                            self.context.project.set_control(draining=True)
                            self.context.handle("_request_done", {"sequence": 1, "failed": True})
                            return {"type": "message_end", "message": {"role": "assistant",
                                    "stopReason": "error", "errorMessage": "fake provider failure"}}
                        try:
                            self.context.handle("_budget", {"sequence": 1})
                        except DeadlineWrapUp:
                            pass
                        else:
                            raise AssertionError("drain should refuse the next Pi request")
                        return {"type": "message_end", "message": {"role": "assistant",
                                "stopReason": "error", "errorMessage": "local admission refused"}}
                    def close(self):
                        return None

                # Fake the SDK's error event for bridge rejection and the host
                # Channel's direct safe-boundary signal; neither contacts a provider.
                channel_type = type("BoundChannelStub", (ChannelStub,),
                                    {"config": self.config, "boundary": boundary})
                with patch("mizu.pi.Bridge", BridgeStub), patch("mizu.pi.Channel", channel_type), \
                     patch("mizu.pi.environment", return_value={}), patch("mizu.pi.credentials", return_value={}):
                    error = ModelFailure if boundary == "provider_error" else DeadlineWrapUp
                    with self.assertRaises(error):
                        PiDriver(self.config).execute(ctx, "Finish after drain")
                self.assertEqual(getattr(ctx, "drain_admission_refused", False), boundary == "budget")
                if boundary == "provider_error":
                    self.assertIsNone(ctx.bound_hit)
                    self.assertFalse(ctx.request_failure_pending)
                    self.assertFalse(path.exists())
                else:
                    self.assertEqual(read_json(path)["id"], "persistent-drain-session")

    def test_the_durable_launcher_expects_the_same_drain_message(self):
        # The shared handshake is a text, so the host's refusal and the
        # durable launcher's classification of it must be the same string: a
        # change to one side alone would silently misname the bound.
        import re
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        launcher = (root / "adapters" / "pi-durable" / "launcher.mjs").read_text(encoding="utf-8")
        host = (root / "src" / "mizu" / "runtime.py").read_text(encoding="utf-8")
        message = "Drain bound refused the provider request"
        self.assertIn(message, host)
        # The classification site is the function that names the bound, so the
        # refusal text is tied there and not only somewhere in the file.
        start = launcher.index("export function boundStatusFor")
        self.assertIn(message, launcher[start:launcher.index("\n}", start)])
        # Every drain-refusal literal in the launcher is the host's own, so a
        # variant at any site fails the suite.
        variants = set(re.findall(r"['\"]([^'\"]*Drain bound refused[^'\"]*)['\"]", launcher))
        self.assertEqual(variants, {message})

    def test_non_pi_drain_wrap_up_allowance_exhaustion_is_bounded(self):
        ctx = self.context()
        ctx.bound_hit = "drain"
        ctx.wrap_up_attempted = True
        ctx.wrap_up_requests = WRAP_UP_REQUESTS
        with self.assertRaises(DrainYieldExceeded):
            admit_invocation(ctx, unit="query")

    def test_request_admission_catches_boundary_before_transport_poll(self):
        ctx = self.context()
        ctx.handle("_budget", {"sequence": 1})
        self.project.set_control(draining=True)
        ctx.handle("_request_done", {"sequence": 1})
        with self.assertRaisesRegex(DeadlineWrapUp, "Drain bound refused the provider request"):
            ctx.handle("_budget", {"sequence": 2})
        self.assertEqual(ctx.request_count, 1)
        self.assertFalse(ctx.request_deadlines)
        self.assertIsNone(ctx.admission_error)
        # The durable launcher's existing settled request_bound handshake.
        ctx.model_evidence = {}
        with self.assertRaises(DeadlineWrapUp):
            interpret_result({"durable_result": True, "status": "request_bound"}, ctx, {}, None)

    def test_latched_boundary_fences_dispatch_until_reduced_grant_wrap_up(self):
        ctx = self.context()
        self.project.set_control(draining=True)
        ctx.enter_drain_wrap_up()
        self.assertEqual(ctx.bound_hit, "drain")
        with patch.object(ctx, "dispatch") as dispatch:
            with self.assertRaises(Denied):
                ctx.handle("finish", FINISH)
            dispatch.assert_not_called()
        ctx.wrap_up_attempted = True
        ctx.handle("finish", FINISH)
        self.assertEqual(ctx.finished["outcome"], "continue")

    def test_only_persistent_non_ephemeral_enabled_runs_yield(self):
        for session, ephemeral, enabled, yields in (
                ("persistent", False, True, True), ("persistent", False, False, False),
                ("persistent", True, True, False), ("ephemeral", False, True, False)):
            with self.subTest(session=session, ephemeral=ephemeral, enabled=enabled):
                ctx = self.context()
                ctx.ephemeral = ephemeral
                ctx.config = dataclasses.replace(self.config,
                    limits=dataclasses.replace(self.config.limits, drain_yield=enabled),
                    profiles={**self.config.profiles, ctx.role.profile: {
                        **self.config.profiles[ctx.role.profile], "session": session}})
                self.project.set_control(draining=True)
                if yields:
                    with self.assertRaises(DeadlineWrapUp):
                        ctx.check_deadlines(wrap_up=True)
                else:
                    ctx.check_deadlines(wrap_up=True)
                    ctx.handle("_budget", {"sequence": 1})
                    ctx.handle("finish", FINISH)
                    self.assertIsNone(ctx.bound_hit)
                    self.assertEqual(ctx.finished["outcome"], "continue")

    def test_dispatch_and_join_block_the_transition(self):
        for event in ("operation", "join", "mutex"):
            with self.subTest(event=event):
                ctx = self.context()
                self.project.set_control(draining=True)
                ready, release = threading.Event(), threading.Event()
                def admitted_work():
                    with ctx.mutex:
                        if event == "operation":
                            ctx.operation_active.set()
                        if event == "join":
                            ctx.joins_idle.clear()
                        ready.set()
                        release.wait(5)
                        ctx.operation_active.clear()
                        ctx.joins_idle.set()
                thread = threading.Thread(target=admitted_work)
                thread.start()
                self.assertTrue(ready.wait(5))
                try:
                    ctx.check_deadlines(wrap_up=True)  # Must not block on mutex.
                    self.assertIsNone(ctx.bound_hit)
                finally:
                    release.set()
                    thread.join(5)
                self.assertFalse(thread.is_alive())
                with self.assertRaises(DeadlineWrapUp):
                    ctx.check_deadlines(wrap_up=True)

    def test_ordinary_queued_waiter_does_not_yield(self):
        resource = self.project.root / "locks/run-worker.lock"
        pending = _Queue(self.config, [resource])
        try:
            with lock(resource, blocking=False):
                with self.assertRaises(Busy):
                    pending.attempt(enqueue=True)
                ctx = self.context()
                ctx.check_deadlines(wrap_up=True)
                ctx.handle("_budget", {"sequence": 1})
                self.assertIsNone(ctx.bound_hit)
        finally:
            pending.close()

    def start_drain(self, ctx):
        self.project.set_control(draining=True)
        ctx.handle("_request_done", {"sequence": 1})
        ctx.check_deadlines(wrap_up=True)

    def test_successful_durable_seal_preserves_session_workspace_and_releases_locks(self):
        self.config = dataclasses.replace(self.config, profiles={**self.config.profiles,
            "primary": {**self.config.profiles["primary"], "engine": "pi-durable", "options": {}}})
        driver = PiDurableDriver(self.config)
        driver.requires_sandbox = False
        dispatches = []
        def turn(ctx, settings, model, prompt, profile, run_key, store_path):
            dispatches.append((ctx, run_key, store_path, ctx._durable_request_id))
            ctx.handle("_budget", {"sequence": 1})
            if len(dispatches) == 1:
                (ctx.workspace / "app.py").write_text("VALUE = 3\n")
                self.project.set_control(draining=True)
                ctx.check_deadlines(wrap_up=True)  # Still inside the provider request.
                self.assertIsNone(ctx.bound_hit)
                ctx.handle("_request_done", {"sequence": 1})
                ctx.check_deadlines(wrap_up=True)
            self.assertEqual((ctx.workspace / "app.py").read_text(), "VALUE = 3\n")
            if len(dispatches) == 2:
                self.assertEqual(ctx.role.engine_tools, ())
                self.assertNotIn("exec", ctx.role.capabilities)
                self.assertEqual(ctx.wrap_up_requests, 1)
            ctx.handle("finish", FINISH)
            ctx.handle("_request_done", {"sequence": 1})
            return {"engine": "pi-durable", "requests": ctx.request_count}
        driver._run_once = turn
        engine = Engine(self.config, driver=driver)
        first = engine.run(self.project, "worker")
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["finish"]["outcome"], "continue")
        self.assertEqual(first["wrap_up"], {"bound": "drain"})
        self.assertEqual(dispatches[0][1:3], dispatches[1][1:3])
        self.assertEqual(dispatches[1][3], dispatches[0][3] + "-t2")
        ctx = dispatches[0][0]
        path, saved = session_record(ctx, "primary", effective(self.config, ctx.role, "primary"))
        self.assertIsNotNone(saved)
        with lock(self.project.root / "locks/run-worker.lock", blocking=False), \
                lock(self.project.root / "locks/workspace.lock", blocking=False):
            self.assertFalse((self.project.root / "active/worker.json").exists())
        self.project.set_control(draining=False)
        second = engine.run(self.project, "worker")
        self.assertEqual(second["status"], "completed")
        next_ctx = dispatches[-1][0]
        next_path, next_saved = session_record(next_ctx, "primary", effective(self.config, next_ctx.role, "primary"))
        self.assertEqual(next_path, path)
        self.assertEqual(next_saved["id"], saved["id"])
        self.assertEqual(dispatches[-1][2], dispatches[0][2])
        self.assertNotEqual(dispatches[-1][1], dispatches[0][1])
        self.assertEqual(read_json(next_ctx.run_dir / "prompt_projection.json")["prompt_mode"], "delta")
        conn = open_store(dispatches[0][2])
        try:
            self.assertTrue(load_run(conn, first["run"])["result"]["sealed"])
        finally:
            conn.close()

    def test_non_durable_persistent_wrap_up_keeps_session_identity(self):
        paths = []
        def work(ctx, *_):
            path, _ = session_record(ctx, ctx.role.profile, effective(ctx.config, ctx.role, ctx.role.profile))
            paths.append(path)
            save_session(path, "fake-session", {})
            if len(driver.calls) == 1:
                self.start_drain(ctx)
            ctx.handle("finish", FINISH)
        driver = UnsealedDriver(work)
        result = Engine(self.config, driver=driver).run(self.project, "worker")
        self.assertEqual(result["finish"]["outcome"], "continue")
        self.assertEqual(paths[0], paths[1])

    def test_unsealed_wrap_up_is_durable_non_failure_without_publication(self):
        for ending in ("return", "requests", "tools", "durable_requests", "wall_return"):
            with self.subTest(ending=ending):
                self.project.set_control(draining=False)
                before = self.project.snapshots.get()
                health = {"consecutive_failures": 2, "last_run": "prior"}
                write_json(self.project.root / "health/worker.json", health)
                def work(ctx, *_):
                    (ctx.workspace / "app.py").write_text("unsealed work\n")
                    if len(driver.calls) == 1:
                        if ending == "return":
                            self.project.set_control(draining=True)
                            return  # The normal return retires the request before Engine checks.
                        self.start_drain(ctx)
                    self.assertTrue(ctx.wrap_up_attempted)
                    if ending in ("requests", "durable_requests"):
                        for sequence in range(2, WRAP_UP_REQUESTS + 1):
                            ctx.handle("_budget", {"sequence": sequence})
                        if ending == "durable_requests":
                            with self.assertRaises(LimitExceeded):
                                ctx.handle("_budget", {"sequence": 20})
                            ctx.model_evidence = {}
                            interpret_result({"durable_result": True, "status": "request_bound"}, ctx, {}, None)
                        else:
                            ctx.handle("_budget", {"sequence": 20})
                    if ending == "wall_return":
                        ctx.handle("_request_done", {"sequence": 1})
                        ctx.deadline = 0
                    if ending == "tools":
                        with patch.object(ctx, "dispatch", return_value={}):
                            for _ in range(WRAP_UP_TOOLS + 1):
                                ctx.handle("finish", FINISH)
                driver = UnsealedDriver(work)
                result = Engine(self.config, driver=driver).run(self.project, "worker")
                ctx = driver.calls[-1][0]
                self.assertEqual(result["status"], "bounded")
                self.assertEqual(result["bound"], "deadline" if ending == "wall_return" else "drain")
                self.assertEqual(result["publication"], "not_published")
                self.assertEqual(read_json(ctx.run_dir / "result.json"), result)
                self.assertFalse((ctx.run_dir / "error.json").exists())
                if ending != "wall_return":
                    self.assertIsNone(ctx.missing_finish_evidence)
                self.assertEqual(self.project.snapshots.get()["id"], before["id"])
                self.assertEqual(read_json(self.project.root / "health/worker.json"), health)

    def test_drain_faults_keep_their_classification(self):
        for fault in (ModelFailure("pi", kind="error", message="fake provider fault"),
                      ProtocolError("fake protocol fault"), OSError(errno.EIO, "fake storage fault"),
                      LimitExceeded("fake unrelated limit"), InfraExceeded("fake daily budget"),
                      Cancelled("fake cancellation")):
            with self.subTest(fault=type(fault).__name__):
                self.project.set_control(draining=False, paused=False)
                write_json(self.project.root / "health/worker.json", {"consecutive_failures": 0})
                def work(ctx, *_):
                    if len(driver.calls) == 1:
                        self.start_drain(ctx)
                    raise fault
                driver = UnsealedDriver(work)
                with self.assertRaises(type(fault)) as raised:
                    Engine(self.config, driver=driver).run(self.project, "worker")
                self.assertIs(raised.exception, fault)
                ctx = driver.calls[-1][0]
                self.assertIsNone(ctx.missing_finish_evidence)
                self.assertEqual(read_json(ctx.run_dir / "error.json")["error"], str(fault))

    def test_host_cancellation_keeps_priority_before_and_during_wrap_up(self):
        for control in ("stop", "pause", "disarm"):
            for during_wrap_up in (False, True):
                with self.subTest(control=control, wrap_up=during_wrap_up):
                    self.project.set_control(draining=False, paused=False, armed=True)
                    def work(ctx, *_):
                        self.project.set_control(draining=True)
                        if during_wrap_up and len(driver.calls) == 1:
                            self.start_drain(ctx)
                        if control == "stop":
                            ctx.stop.set()
                        elif control == "pause":
                            self.project.set_control(paused=True)
                        else:
                            self.project.set_control(armed=False)
                        ctx.check_deadlines(wrap_up=True)
                    driver = UnsealedDriver(work)
                    with self.assertRaises(Cancelled):
                        Engine(self.config, driver=driver).run(self.project, "worker")
                    self.assertIsNone(driver.calls[-1][0].missing_finish_evidence)

    def test_active_request_timeout_is_not_a_drain_bound(self):
        ctx = self.context()
        ctx.deadline = ctx.wrap_up_at = None
        ctx.handle("_budget", {"sequence": 1})
        deadline = next(iter(ctx.request_deadlines.values()))
        self.project.set_control(draining=True)
        with patch("mizu.runtime.time.monotonic", return_value=deadline + 1):
            with self.assertRaisesRegex(LimitExceeded, "Logical model request") as raised:
                ctx.check_deadlines(wrap_up=True)
        self.assertNotIsInstance(raised.exception, RunDeadlineExceeded)
        self.assertIsNone(ctx.bound_hit)

    def test_malformed_unsealed_wrap_up_is_protocol_failure(self):
        def work(ctx, *_):
            if len(driver.calls) == 1:
                self.start_drain(ctx)
            ctx.operations_stopped.set()  # Fake terminal return with an unretired request.
        driver = UnsealedDriver(work)
        with self.assertRaisesRegex(ProtocolError, "active model-request deadline"):
            Engine(self.config, driver=driver).run(self.project, "worker")
        self.assertIsNone(driver.calls[-1][0].missing_finish_evidence)
        self.assertEqual(read_json(self.project.root / "health/worker.json")["consecutive_failures"], 1)

    def test_final_publication_failure_after_drain_seal_stays_a_failure(self):
        def work(ctx, *_):
            if len(driver.calls) == 1:
                self.start_drain(ctx)
            ctx.handle("finish", FINISH)
        driver = UnsealedDriver(work)
        fault = OSError(errno.EIO, "fake publication fault")
        with patch.object(self.project.snapshots, "publish", side_effect=fault):
            with self.assertRaises(OSError) as raised:
                Engine(self.config, driver=driver).run(self.project, "worker")
        self.assertIs(raised.exception, fault)
        self.assertIsNone(driver.calls[-1][0].missing_finish_evidence)
        self.assertEqual(read_json(self.project.root / "health/worker.json")["consecutive_failures"], 1)

    def test_bounded_result_storage_failure_is_a_failure(self):
        def work(ctx, *_):
            if len(driver.calls) == 1:
                self.start_drain(ctx)
        driver = UnsealedDriver(work)
        fault = OSError(errno.EIO, "fake result storage fault")
        def record(path, value):
            if path.name == "result.json" and value.get("status") == "bounded":
                raise fault
            return write_json(path, value)
        with patch("mizu.runtime.write_json", side_effect=record):
            with self.assertRaises(OSError) as raised:
                Engine(self.config, driver=driver).run(self.project, "worker")
        self.assertIs(raised.exception, fault)
        self.assertEqual(read_json(self.project.root / "health/worker.json")["consecutive_failures"], 1)
        self.assertEqual(read_json(driver.calls[-1][0].run_dir / "error.json")["error"], str(fault))
