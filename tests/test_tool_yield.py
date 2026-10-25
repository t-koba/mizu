"""Host tool-reserve refusals through fake Pi RPC, not live-provider evidence."""
import contextlib
import dataclasses
from unittest.mock import patch

from support import Fixture
from mizu.errors import (DrainYieldExceeded, LimitExceeded, ModelFailure,
                         RequestYieldExceeded, RunDeadlineExceeded, ToolYieldExceeded)
from mizu.fs import read_json
from mizu.pi import PiDriver
from mizu.runtime import Engine, WRAP_UP_OPS

FINISH = {'outcome': 'wait', 'summary': 'Saved result', 'state': 'Next: review.'}


class ToolYieldTests(Fixture):
    def configure_tools(self):
        self.config = dataclasses.replace(self.config,
            limits=dataclasses.replace(self.config.limits, tools_per_run=0, wrap_up_tools=1))

    def exhaust(self, ctx, bound='tools'):
        ctx.bound_hit = bound
        ctx.wrap_up_attempted = True
        ctx.wrap_up_tools = 1
        with self.assertRaises(LimitExceeded) as caught:
            ctx.handle('finish', FINISH)
        self.assertIs(caught.exception, ctx.tool_admission_exception)
        self.assertIsNone(ctx.finished)
        return caught.exception

    @contextlib.contextmanager
    def channel(self, action, *, settled=False, message='provider error'):
        """Real Context handles admission/refusal before a synthetic Pi event."""
        class BridgeStub:
            def __init__(self, handle, *args, **kwargs):
                self.config_file = handle.__self__.run_dir / 'bridge.json'
            def __enter__(self): return self
            def __exit__(self, *args): pass

        class ChannelStub:
            def __init__(self, context, *args, **kwargs):
                self.ctx = context
                self.count = 0
            def send(self, event): pass
            def close(self): pass
            def receive(self, **kwargs):
                self.count += 1
                ctx = self.ctx
                if self.count == 1:
                    ctx.handle('_hello', {})
                    model = ctx.config.model(ctx.role.profile)
                    return {'id': 'state', 'success': True, 'data': {
                        'model': {'id': model['model'], 'provider': model['provider']},
                        'sessionFile': 'tool-bound-session'}}
                if self.count > 2:
                    raise AssertionError('Unexpected extra Pi event read')
                ctx.handle('_budget', {'sequence': 1})
                ctx.handle('_request_done', {'sequence': 1})
                action(ctx)
                if settled:
                    return {'type': 'agent_settled'}
                return {'type': 'message_end', 'message': {'role': 'assistant',
                    'stopReason': 'error', 'errorMessage': message}}

        with patch('mizu.pi.Bridge', BridgeStub), patch('mizu.pi.Channel', ChannelStub):
            yield

    def driver(self):
        driver = PiDriver(self.config)
        driver.requires_sandbox = False  # Channel is fake; no process is launched.
        return driver

    def test_actual_reserve_refusal_survives_pi_error_for_each_bound(self):
        self.configure_tools()
        for bound, exception in (('tools', ToolYieldExceeded),
                                 ('requests', RequestYieldExceeded),
                                 ('deadline', RunDeadlineExceeded),
                                 ('drain', DrainYieldExceeded)):
            with self.subTest(bound=bound):
                ctx = self.context()
                observed = []
                def refuse(ctx):
                    observed.append(self.exhaust(ctx, bound))
                driver = self.driver()
                with self.channel(refuse), self.assertRaises(exception) as caught:
                    Engine(self.config, driver=driver)._dispatch(ctx, driver, '{}')
                self.assertIs(caught.exception, observed[0])
                self.assertIsInstance(caught.exception.__cause__, ModelFailure)
                self.assertIsNone(ctx.admission_exception)
                self.assertEqual(ctx.wrap_up_tools, 1)  # Refusal spends nothing.

    def test_initial_tool_cap_keeps_existing_reduced_grant_followup(self):
        self.configure_tools()
        self.config = dataclasses.replace(self.config,
            limits=dataclasses.replace(self.config.limits, tools_per_run=1))
        calls = []
        def act(ctx):
            calls.append((ctx.execution_attempt, ctx.wrap_up_attempted, ctx.role))
            if not ctx.wrap_up_attempted:
                ctx.handle('files', {'offset': 0, 'limit': 1})
                with self.assertRaises(LimitExceeded) as caught:
                    ctx.handle('files', {'offset': 0, 'limit': 1})
                self.assertIs(type(caught.exception), LimitExceeded)
                self.assertEqual(ctx.wrap_up_tools, 0)
            else:
                self.assertTrue(set(ctx.role.capabilities) <= WRAP_UP_OPS)
                self.assertEqual(ctx.role.engine_tools, ())
                ctx.handle('finish', FINISH)
        # The first error event must reach Engine's LimitExceeded follow-up,
        # not provider-fault handling. The second event settles a real seal.
        class FollowupDriver(PiDriver):
            requires_sandbox = False
            def execute(inner, ctx, prompt, **kwargs):
                with self.channel(act, settled=ctx.wrap_up_attempted):
                    return super().execute(ctx, prompt, **kwargs)
        result = Engine(self.config, driver=FollowupDriver(self.config)).run(self.project, 'worker')
        self.assertEqual(len(calls), 2)
        self.assertEqual([call[1] for call in calls], [False, True])
        self.assertEqual(calls[1][0], calls[0][0] + 1)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['wrap_up'], {'bound': 'tools'})
        self.assertEqual(self.project.snapshots.get()['outcome'], 'bounded')

    def test_error_and_normal_unsealed_settlement_record_bounded_without_publication(self):
        self.configure_tools()
        for settled in (False, True):
            with self.subTest(settled=settled):
                seen = []
                def refuse(ctx):
                    (ctx.workspace / 'app.py').write_text('VALUE = 3\n')
                    seen.append(ctx)
                    self.exhaust(ctx)
                before = self.project.snapshots.get()['id']
                health_path = self.project.root / 'health/worker.json'
                health_before = read_json(health_path, {})
                with self.channel(refuse, settled=settled):
                    result = Engine(self.config, driver=self.driver()).run(self.project, 'worker')
                self.assertEqual(len(seen), 1)  # No missing-finish follow-ups.
                self.assertIsNone(seen[0].missing_finish_evidence)
                self.assertEqual(result['status'], 'bounded')
                self.assertEqual(result['bound'], 'tools')
                self.assertEqual(result['publication'], 'not_published')
                self.assertEqual(read_json(seen[0].run_dir / 'result.json', {}), result)
                self.assertEqual(self.project.snapshots.get()['id'], before)
                self.assertEqual((self.project.workspace / 'app.py').read_text(), 'VALUE = 3\n')
                self.assertEqual(read_json(health_path, {}), health_before)
                self.assertFalse(self.project.control()['paused'])

    def test_successful_admission_or_operation_clears_refusal_before_provider_error(self):
        self.configure_tools()
        for continuation in ('admission', 'operation'):
            with self.subTest(continuation=continuation):
                ctx = self.context()
                def continue_work(ctx):
                    self.exhaust(ctx)
                    if continuation == 'admission':
                        ctx.handle('_budget', {'sequence': 2})
                        ctx.handle('_request_done', {'sequence': 2})
                    else:
                        # Explicitly enlarge the configured allowance in this
                        # test so actual successful continued work is possible.
                        ctx.role = dataclasses.replace(ctx.role, wrap_up_tools=2,
                            capabilities=(*ctx.role.capabilities, 'research'))
                        ctx.handle('research', {'state': '{}', 'expected_generation': 0})
                    self.assertIsNone(ctx.tool_admission_exception)
                driver = self.driver()
                with self.channel(continue_work), self.assertRaises(ModelFailure):
                    Engine(self.config, driver=driver)._dispatch(ctx, driver, '{}')

    def test_new_dispatch_clears_refusal_and_provider_text_cannot_forge_it(self):
        self.configure_tools()
        ctx = self.context()
        driver = self.driver()
        with self.channel(self.exhaust), self.assertRaises(ToolYieldExceeded):
            Engine(self.config, driver=driver)._dispatch(ctx, driver, '{}')
        self.assertIsInstance(ctx.tool_admission_exception, ToolYieldExceeded)
        # Observe reset before this invocation's successful admission can clear it.
        class ProviderOnly:
            def execute(inner, ctx, prompt, **kwargs):
                self.assertIsNone(ctx.tool_admission_exception)
                raise ModelFailure('pi', kind='error', message='Tool wrap-up allowance exhausted')
        with self.assertRaises(ModelFailure):
            Engine(self.config)._dispatch(ctx, ProviderOnly(), '{}')
        fresh = self.context()
        with self.channel(lambda ctx: None, message='Per-run tool limit exceeded'), self.assertRaises(ModelFailure):
            Engine(self.config, driver=driver)._dispatch(fresh, driver, '{}')
        self.assertIsNone(fresh.tool_admission_exception)
