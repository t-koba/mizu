"""Offline local-admission boundaries; no provider or deployment claims."""
import dataclasses
import time
from unittest.mock import patch
from support import Fixture
from test_deadlines import UnsealedDriver
from mizu.config import role_requests_per_run
from mizu.errors import (DeadlineWrapUp, RequestYieldExceeded, InfraExceeded,
                         ModelFailure, RunDeadlineExceeded)
from mizu.fs import read_json
from mizu.runtime import Engine, WRAP_UP_OPS


class RequestYieldTests(Fixture):
    def test_normal_requests_do_not_spend_wrapup_allowance(self):
        ctx = self.context()
        ctx.request_count = role_requests_per_run(self.config, ctx.role)
        with self.assertRaises(DeadlineWrapUp):
            ctx.handle('_budget', {'sequence': 1})
        self.assertEqual(ctx.wrap_up_requests, 0)
        self.assertFalse(ctx.request_deadlines)
        self.assertEqual(ctx.bound_hit, 'requests')
        ctx.wrap_up_attempted = True
        ctx.handle('_budget', {'sequence': 1})
        self.assertEqual(ctx.wrap_up_requests, 1)
        ctx.handle('_request_done', {'sequence': 1})
        ctx.wrap_up_requests = ctx._wrap_up_request_used_limit()[1]
        with self.assertRaises(RequestYieldExceeded):
            ctx.handle('_budget', {'sequence': 2})

    def test_daily_quota_is_not_a_request_yield(self):
        ctx = self.context()
        with patch('mizu.runtime.Budget.take', side_effect=InfraExceeded('daily quota')):
            with self.assertRaises(InfraExceeded):
                ctx.handle('_budget', {'sequence': 1})
        self.assertIsNone(ctx.bound_hit)
        self.assertFalse(getattr(ctx, 'request_admission_refused', False))

    def test_unsealed_wrapup_preserves_work_without_failure_or_publication(self):
        before = self.project.snapshots.get()['id']
        def work(ctx, prompt, profile=None):
            if not ctx.wrap_up_attempted:
                (ctx.workspace / 'app.py').write_text('VALUE = 3\n')
                ctx.request_count = role_requests_per_run(self.config, ctx.role)
                ctx.handle('_budget', {'sequence': 1})
            else:
                ctx.handle('_budget', {'sequence': 1})
                ctx.handle('_request_done', {'sequence': 1})
                return {'requests': 1}
        driver = UnsealedDriver(work)
        result = Engine(self.config, driver=driver).run(self.project, 'worker')
        self.assertEqual(result['status'], 'bounded')
        self.assertEqual(result['bound'], 'requests')
        self.assertEqual(result['publication'], 'not_published')
        self.assertEqual(self.project.snapshots.get()['id'], before)
        self.assertEqual((self.project.workspace / 'app.py').read_text(), 'VALUE = 3\n')
        self.assertFalse(self.project.control()['paused'])
        health = read_json(self.project.root / 'health/worker.json', {})
        self.assertEqual(health.get('consecutive_failures', 0), 0)

    def test_pi_error_event_from_local_bound_enters_wrapup(self):
        from mizu.pi import PiDriver
        ctx = self.context()
        ctx.request_count = role_requests_per_run(self.config, ctx.role)
        model = self.config.model(ctx.role.profile)
        class BridgeStub:
            def __init__(self, *args, **kwargs): self.config_file = ctx.run_dir / 'bridge.json'
            def __enter__(self): return self
            def __exit__(self, *args): pass
        class ChannelStub:
            def __init__(self, *args, **kwargs): self.count = 0
            def send(self, event): pass
            def close(self): pass
            def receive(self, **kwargs):
                self.count += 1
                if self.count == 1:
                    ctx.handle('_hello', {})
                    return {'id':'state','success':True,'data':{
                        'model':{'id':model['model'],'provider':model['provider']},
                        'sessionFile':'request-bound-session'}}
                try: ctx.handle('_budget', {'sequence':1})
                except DeadlineWrapUp: pass
                return {'type':'message_end','message':{'role':'assistant',
                    'stopReason':'error','errorMessage':'local admission refused'}}
        with patch('mizu.pi.Bridge', BridgeStub), patch('mizu.pi.Channel', ChannelStub):
            with self.assertRaises(DeadlineWrapUp):
                PiDriver(self.config).execute(ctx, '{}', profile=ctx.role.profile)
        self.assertEqual(ctx.wrap_up_requests, 0)

    def test_pi_deadline_cutoff_yields_before_reserve_and_uses_host_evidence(self):
        from mizu.pi import PiDriver

        def dispatch_error(ctx, *, refuse):
            model = self.config.model(ctx.role.profile)

            class BridgeStub:
                def __init__(self, *args, **kwargs):
                    self.config_file = ctx.run_dir / 'bridge.json'
                def __enter__(self): return self
                def __exit__(self, *args): pass

            class ChannelStub:
                def __init__(self, *args, **kwargs): self.count = 0
                def send(self, event): pass
                def close(self): pass
                def receive(self, **kwargs):
                    self.count += 1
                    if self.count == 1:
                        ctx.handle('_hello', {})
                        return {'id':'state','success':True,'data':{
                            'model':{'id':model['model'],'provider':model['provider']},
                            'sessionFile':'deadline-cutoff-session'}}
                    if refuse:
                        try:
                            ctx.handle('_budget', {'sequence':1})
                        except DeadlineWrapUp:
                            pass
                    return {'type':'message_end','message':{'role':'assistant',
                        'stopReason':'error','errorMessage':'provider text is not local evidence'}}

            driver = PiDriver(self.config)
            with patch('mizu.pi.Bridge', BridgeStub), patch('mizu.pi.Channel', ChannelStub):
                return Engine(self.config, driver=driver)._dispatch(
                    ctx, driver, '{}', profile=ctx.role.profile)

        local = self.context()
        local.wrap_up_at = time.monotonic() - 1  # Past; absolute deadline remains live.
        self.assertFalse(local.wall_expired())
        with self.assertRaises(DeadlineWrapUp) as raised:
            dispatch_error(local, refuse=True)
        self.assertIs(raised.exception, local.admission_exception)
        self.assertEqual(local.bound_hit, 'deadline')
        self.assertFalse(local.wrap_up_attempted)
        self.assertEqual((local.request_count, local.wrap_up_requests), (0, 0))
        self.assertFalse((local.run_dir / 'admission.json').exists())

        provider = self.context()
        with self.assertRaises(ModelFailure):
            dispatch_error(provider, refuse=False)
        self.assertIsNone(provider.admission_exception)

        # Only Engine's dedicated follow-up phase may spend the reserve.
        local.wrap_up_attempted = True
        local.handle('_budget', {'sequence':1})
        self.assertEqual((local.request_count, local.wrap_up_requests), (1, 1))

    def test_pi_exhausted_request_reserves_survive_error_event_and_dispatch(self):
        from mizu.pi import PiDriver

        def dispatch_error(ctx):
            model = self.config.model(ctx.role.profile)

            class BridgeStub:
                def __init__(self, *args, **kwargs):
                    self.config_file = ctx.run_dir / "bridge.json"
                def __enter__(self): return self
                def __exit__(self, *args): pass

            class ChannelStub:
                def __init__(self, *args, **kwargs): self.count = 0
                def send(self, event): pass
                def close(self): pass
                def receive(self, **kwargs):
                    self.count += 1
                    if self.count == 1:
                        ctx.handle("_hello", {})
                        return {"id": "state", "success": True, "data": {
                            "model": {"id": model["model"], "provider": model["provider"]},
                            "sessionFile": "request-reserve-session"}}
                    if self.count != 2:
                        raise AssertionError("Unexpected extra Pi event read")
                    try:
                        ctx.handle("_budget", {"sequence": 1})
                    except (RequestYieldExceeded, RunDeadlineExceeded):
                        pass
                    return {"type": "message_end", "message": {"role": "assistant",
                        "stopReason": "error", "errorMessage": "provider-like error text"}}

            driver = PiDriver(self.config)
            with patch("mizu.pi.Bridge", BridgeStub), patch("mizu.pi.Channel", ChannelStub):
                return Engine(self.config, driver=driver)._dispatch(
                    ctx, driver, "{}", profile=ctx.role.profile)

        for bound, error in (("requests", RequestYieldExceeded),
                             ("deadline", RunDeadlineExceeded)):
            with self.subTest(bound=bound):
                ctx = self.context()
                ctx.bound_hit = bound
                ctx.wrap_up_attempted = True
                if bound == "requests":
                    ctx.request_count = role_requests_per_run(self.config, ctx.role)
                initial_count = ctx.request_count
                ctx.wrap_up_requests = ctx._wrap_up_request_used_limit()[1]
                # The exhausted in-RPC refusal must not reach daily admission.
                with patch("mizu.runtime.Budget.take") as take:
                    with self.assertRaises(error) as raised:
                        dispatch_error(ctx)
                    take.assert_not_called()
                if bound == "deadline":
                    self.assertIs(raised.exception, ctx.admission_exception)
                self.assertEqual((ctx.request_count, ctx.wrap_up_requests), (initial_count, ctx._wrap_up_request_used_limit()[1]))
                self.assertFalse((ctx.run_dir / "admission.json").exists())

        # The error event alone is not evidence of local request exhaustion.
        provider = self.context()
        model = self.config.model(provider.role.profile)
        class BridgeStub:
            def __init__(self, *args, **kwargs): self.config_file = provider.run_dir / "bridge.json"
            def __enter__(self): return self
            def __exit__(self, *args): pass
        class ChannelStub:
            def __init__(self, *args, **kwargs): self.count = 0
            def send(self, event): pass
            def close(self): pass
            def receive(self, **kwargs):
                self.count += 1
                if self.count == 1:
                    provider.handle("_hello", {})
                    return {"id": "state", "success": True, "data": {
                        "model": {"id": model["model"], "provider": model["provider"]},
                        "sessionFile": "provider-error-session"}}
                return {"type": "message_end", "message": {"role": "assistant",
                    "stopReason": "error", "errorMessage": "provider text only"}}
        driver = PiDriver(self.config)
        with patch("mizu.pi.Bridge", BridgeStub), patch("mizu.pi.Channel", ChannelStub):
            with self.assertRaises(ModelFailure):
                Engine(self.config, driver=driver)._dispatch(provider, driver, "{}", profile=provider.role.profile)
        self.assertIsNone(provider.admission_exception)

    def test_durable_bound_settlement_preserves_wrapup_allowance(self):
        from mizu.pi_durable import interpret_result
        ctx = self.context()
        ctx.bound_hit = 'requests'
        ctx.request_admission_refused = True
        report = {'durable_result':True, 'status':'request_bound'}
        with patch('mizu.pi_durable._dispatch_usage', return_value={}), \
                patch('mizu.pi_durable._record_launcher_evidence'):
            with self.assertRaises(DeadlineWrapUp):
                interpret_result(report, ctx, {}, None)
            ctx.wrap_up_attempted = True
            ctx.request_bound_exhausted = True
            with self.assertRaises(RequestYieldExceeded):
                interpret_result(report, ctx, {}, None)


class NonPiRequestCeilingTests(Fixture):
    def test_cap_yields_before_daily_charge_for_query_and_turn(self):
        from mizu.drivers import admit_invocation
        for unit in ("query", "turn"):
            with self.subTest(unit=unit):
                ctx = self.context()
                cap = role_requests_per_run(self.config, ctx.role)
                ctx.request_count = cap
                with patch("mizu.drivers.Budget.take") as take:
                    with self.assertRaises(DeadlineWrapUp):
                        admit_invocation(ctx, unit=unit)
                    take.assert_not_called()
                self.assertEqual(ctx.bound_hit, "requests")
                self.assertEqual(ctx.request_count, cap)
                self.assertEqual(ctx.wrap_up_requests, 0)
                self.assertFalse(ctx.request_deadlines)
                self.assertFalse((ctx.run_dir / "admission.json").exists())
                # Engine's reduced-grant follow-up owns the reserve.
                ctx.wrap_up_attempted = True
                ctx.execution_attempt += 1
                with patch("mizu.drivers.Budget.take", return_value=1) as take:
                    self.assertEqual(admit_invocation(ctx, unit=unit), {"admitted": True})
                    self.assertEqual(take.call_count, 1)
                self.assertEqual(ctx.wrap_up_requests, 1)
                self.assertEqual(ctx.request_count, cap + 1)
                # Close the admitted request timer like a settled turn.
                ctx.end_request(0)
                # Next attempt exhausts without another daily charge.
                ctx.execution_attempt += 1
                ctx.wrap_up_requests = ctx._wrap_up_request_used_limit()[1]
                with patch("mizu.drivers.Budget.take") as take:
                    with self.assertRaises(RequestYieldExceeded) as raised:
                        admit_invocation(ctx, unit=unit)
                    take.assert_not_called()
                self.assertIs(type(raised.exception), RequestYieldExceeded)
                self.assertEqual(ctx.wrap_up_requests, ctx._wrap_up_request_used_limit()[1])
                self.assertEqual(ctx.request_count, cap + 1)

    def test_under_cap_daily_infra_stays_infra(self):
        from mizu.drivers import admit_invocation
        ctx = self.context()
        self.assertLess(ctx.request_count, role_requests_per_run(self.config, ctx.role))
        with patch("mizu.drivers.Budget.take", side_effect=InfraExceeded("daily quota")):
            with self.assertRaises(InfraExceeded):
                admit_invocation(ctx, unit="query")
        self.assertIsNone(ctx.bound_hit)
        self.assertEqual(ctx.wrap_up_requests, 0)

    def test_engine_unsealed_uses_one_ordinary_then_one_reduced_grant(self):
        from mizu.drivers import admit_invocation
        before = self.project.snapshots.get()["id"]

        class NonPiUnsealedDriver:
            requires_sandbox = False

            def __init__(self):
                self.calls = []

            def execute(self, context, prompt, *, profile=None):
                self.calls.append((context.execution_attempt, context.wrap_up_attempted,
                                   context.role.capabilities, context.role.engine_tools))
                if not context.wrap_up_attempted:
                    (context.workspace / "app.py").write_text("VALUE = 3\n")
                    context.request_count = role_requests_per_run(
                        context.config, context.spend_role or context.role)
                    admit_invocation(context, unit="query")
                    raise AssertionError("capped invocation must yield before spending reserve")
                admit_invocation(context, unit="query")
                context.end_request(0)
                return {"requests": 1}

        driver = NonPiUnsealedDriver()
        result = Engine(self.config, driver=driver).run(self.project, "worker")
        self.assertEqual(len(driver.calls), 2)
        self.assertEqual(driver.calls[0][1], False)
        self.assertEqual(driver.calls[1][1], True)
        self.assertNotEqual(driver.calls[0][2], driver.calls[1][2])
        self.assertEqual(driver.calls[1][2], tuple(
            capability for capability in driver.calls[0][2] if capability in WRAP_UP_OPS))
        self.assertEqual(driver.calls[1][3], ())
        self.assertEqual(result["status"], "bounded")
        self.assertEqual(result["bound"], "requests")
        self.assertEqual(result["publication"], "not_published")
        self.assertEqual(self.project.snapshots.get()["id"], before)
        self.assertEqual((self.project.workspace / "app.py").read_text(), "VALUE = 3\n")
        self.assertFalse(self.project.control()["paused"])
        health = read_json(self.project.root / "health/worker.json", {})
        self.assertEqual(health.get("consecutive_failures", 0), 0)
