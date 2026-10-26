"""Current engine contracts using synthetic peers, never live inference."""
import dataclasses
from contextlib import nullcontext
import io
import json
import os
import sys
import threading
import time
import unittest
import unittest.mock
from pathlib import Path
from support import Fixture, ROOT
from mizu.bridge import Bridge
from mizu.claude import ClaudeDriver, model_delta, terminal_result
from mizu.codex import CodexDriver, TokenUsage
from mizu.config import load, role_policy_text, system_context_text
from mizu.drivers import DriverCheckpoint, admit_invocation, command_for, driver_for
from mizu.engine_config import effective, session_record, save_session
from mizu.engine_channel import Channel
from mizu.errors import (Cancelled, ConfigError, Denied, LimitExceeded, ProtocolError, ModelFailure,
                         InfraExceeded, RunDeadlineExceeded, DeadlineWrapUp)
from mizu.runtime import Engine
from mizu.fs import mkdir, read_json, write_json
from mizu.mcp_proxy import forward, load_bridge, serve
from mizu.pi import PiDriver
from mizu.process import environment, run as run_process
from mizu.protocol import tool_definitions
from mizu.usage import summarize


def extend_config(fixture, extra, name='alt.toml'):
    path = fixture.root/'config'/name
    path.write_text(fixture.file.read_text()+extra)
    return load(path)


class EngineConfigTests(Fixture):
    def test_routing_and_native_effort(self):
        config = extend_config(self, '\n[profiles.c]\nengine="codex"\nprovider="custom"\nmodel="exact"\nsession="ephemeral"\n[profiles.c.options]\nmodel_reasoning_effort="future-effort"\n')
        self.assertEqual(config.options('c')['model_reasoning_effort'], 'future-effort')
        self.assertIsInstance(driver_for(config, 'c', {}), CodexDriver)
        self.assertIsInstance(driver_for(config, 'primary', {}), PiDriver)

    def test_engine_and_session_are_explicit(self):
        for extra in ('[profiles.c]\nprovider="p"\nmodel="m"\n', '[profiles.c]\nengine="codex"\nprovider="p"\nmodel="m"\nsession="implicit"\n'):
            with self.assertRaises(ConfigError):
                extend_config(self, '\n'+extra)

    def test_empty_command_and_unknown_profile(self):
        path=self.root/'config/empty.toml'
        path.write_text(self.file.read_text().replace('command = ["codex"]', 'command = []'))
        with self.assertRaises(ConfigError): load(path)
        with self.assertRaises(ConfigError): self.config.model('absent')

    def test_owned_options_conflict_before_admission(self):
        ctx = self.context('consult')
        for engine, key in (('pi','modelRuntime'), ('codex','model'), ('claude','can_use_tool')):
            ctx.config = dataclasses.replace(self.config, profiles={**self.config.profiles, ctx.role.profile:
                {**self.config.profiles[ctx.role.profile], 'engine':engine, 'options':{key:'override'}}})
            with self.assertRaises(ConfigError): effective(ctx.config, ctx.role, ctx.role.profile)
            self.assertEqual(ctx.request_count,0)

    def test_claude_verbatim_prompts_is_owned_before_launch_or_admission(self):
        for value in (True, False, 'true', None, 1, {}):
            with self.subTest(value=value):
                ctx = self.context('consult')
                profile = ctx.role.profile
                config = dataclasses.replace(self.config, profiles={**self.config.profiles, profile:
                    {**self.config.profiles[profile], 'engine': 'claude',
                     'options': {'verbatim_prompts': value}}})
                ctx.config = config
                with unittest.mock.patch('mizu.claude.Channel') as channel, \
                     unittest.mock.patch('mizu.claude.Bridge') as bridge, \
                     unittest.mock.patch('mizu.claude.connected_servers') as servers:
                    with self.assertRaisesRegex(ConfigError, 'runtime-owned.*verbatim_prompts'):
                        ClaudeDriver(config).execute(ctx, 'synthetic prompt')
                channel.assert_not_called()
                bridge.assert_not_called()
                servers.assert_not_called()
                self.assertEqual(ctx.request_count, 0)

    def test_shared_admission_idempotent_and_bounded(self):
        ctx=self.context('consult')
        admit_invocation(ctx,unit='turn'); admit_invocation(ctx,unit='turn')
        self.assertEqual(ctx.request_count,1)
        self.assertEqual(read_json(ctx.run_dir/'admission.json')['request_unit'],'turn')
        denied=self.context('consult')
        denied.config=dataclasses.replace(self.config,limits=dataclasses.replace(self.config.limits,daily_requests=0))
        with self.assertRaises(LimitExceeded): admit_invocation(denied,unit='query')
        self.assertEqual(denied.request_count,0)

    def test_session_identity_covers_options_grants_policy_model(self):
        ctx=self.context('consult')
        settings=effective(self.config,ctx.role,'primary')
        original,_=session_record(ctx,'primary',settings)
        for key,value in (('model','changed'),('options',{'thinkingLevel':'new','codemode':True}),('engine_tools',['tool']),('resources',[{'sha256':'a'*64}])):
            changed,_=session_record(ctx,'primary',{**settings,key:value})
            self.assertNotEqual(original,changed)
        save_session(original,'session',{})
        self.assertEqual(session_record(ctx,'primary',settings)[1]['id'],'session')
        write_json(original,{'id':None})
        with self.assertRaises(ConfigError): session_record(ctx,'primary',settings)

    def test_live_effort_shares_session_while_other_options_fork(self):
        from mizu.engine_config import conversation_settings
        ctx=self.context('consult')
        settings=effective(self.config,ctx.role,'primary')
        original,_=session_record(ctx,'primary',settings)
        live,_=session_record(ctx,'primary',{**settings,'options':{**settings['options'],'thinkingLevel':'new'}})
        self.assertEqual(original,live)
        self.assertNotIn('thinkingLevel',conversation_settings(settings)['options'])
        forked,_=session_record(ctx,'primary',{**settings,'options':{**settings['options'],'codemode':True}})
        self.assertNotEqual(original,forked)

    def test_seal_gates_extra_and_nested_operations(self):
        ctx=self.context('consult')
        ctx.role=dataclasses.replace(ctx.role,engine_tools=('tool',))
        self.assertTrue(ctx.handle('_engine_tool',{'name':'tool','invocation_id':'sdk-tool'})['allowed'])
        with self.assertRaises(Denied): ctx.handle('_engine_tool',{'name':'other','invocation_id':'sdk-other'})
        ctx.handle('finish',{'outcome':'wait','summary':'done'})
        with self.assertRaises(Denied): ctx.handle('_engine_tool',{'name':'tool','invocation_id':'sdk-tool'})
        with self.assertRaises(Denied): ctx.handle('_budget',{'sequence':1})


class SyntheticEngineTests(Fixture):
    def execute(self,engine,scenario='normal',persistent=False,context=None):
        ctx=context or self.context('consult')
        profile=ctx.role.profile
        raw={**self.config.profiles[profile], 'engine':engine, 'provider':'custom', 'options':{},
             'session':'persistent' if persistent else 'ephemeral'}
        engines={**self.config.engines,engine:{**self.config.engines[engine],
                  'command':(sys.executable,str(ROOT/'tests/fake_engines.py'),engine,scenario)}}
        config=dataclasses.replace(self.config,profiles={**self.config.profiles,profile:raw},engines=engines)
        ctx.config=config
        driver=CodexDriver(config) if engine=='codex' else ClaudeDriver(config)
        with unittest.mock.patch.dict(os.environ,{'CODEX_AUTH_FILE':str(self.root/'absent')}):
            return ctx,driver.execute(ctx,'synthetic prompt')

    def test_codex_extra_tool_grants_refuse_before_any_launch_or_admission(self):
        for grant in ('web_search', 'spawn_agent', 'mcp__synthetic__search'):
            with self.subTest(grant=grant):
                ctx = self.context('consult')
                ctx.role = dataclasses.replace(ctx.role, engine_tools=(grant,))
                profile = ctx.role.profile
                config = dataclasses.replace(self.config, profiles={**self.config.profiles, profile:
                    {**self.config.profiles[profile], 'engine': 'codex', 'options': {}}})
                ctx.config = config
                with unittest.mock.patch('mizu.codex.Channel') as app_server, \
                     unittest.mock.patch('mizu.codex.connected_servers') as tool_servers, \
                     unittest.mock.patch('mizu.codex.Bridge') as bridge:
                    with self.assertRaisesRegex(ConfigError, 'pre-send'):
                        CodexDriver(config).execute(ctx, 'synthetic prompt')
                app_server.assert_not_called()
                tool_servers.assert_not_called()
                bridge.assert_not_called()
                self.assertEqual(ctx.request_count, 0)
                self.assertEqual(ctx.tool_count, 0)
                self.assertEqual(ctx.engine_tool_invocations, {})
                self.assertEqual(ctx.role.engine_tools, (grant,))

    def test_unexpected_codex_tool_notifications_are_not_admission(self):
        for scenario in ('native-before-finish', 'external-before-finish', 'collab-before-finish'):
            with self.subTest(scenario=scenario):
                ctx = self.context('consult')
                with self.assertRaisesRegex(Denied, 'pre-send boundary'):
                    self.execute('codex', scenario, context=ctx)
                self.assertEqual(ctx.tool_count, 0)
                self.assertEqual(ctx.engine_tool_invocations, {})
                self.assertIsNone(ctx.finished)

    def test_unrelated_codex_thread_tool_notification_does_not_abort_current_turn(self):
        ctx = self.context('consult')
        _evidence = self.execute('codex', 'other-thread-native', context=ctx)
        self.assertEqual(ctx.engine_tool_invocations, {})

    def test_deadline_cutoff_yields_before_non_pi_reserve_and_followup_spends_once(self):
        ctx = self.context('worker')
        ctx.wrap_up_at = time.monotonic() - 1  # Past; absolute deadline remains live.
        self.assertFalse(ctx.wall_expired())
        with self.assertRaises(DeadlineWrapUp):
            admit_invocation(ctx, unit='turn')
        self.assertEqual(ctx.bound_hit, 'deadline')
        self.assertFalse(ctx.wrap_up_attempted)
        self.assertEqual((ctx.request_count, ctx.wrap_up_requests), (0, 0))
        self.assertFalse((ctx.run_dir / 'admission.json').exists())

        ctx.wrap_up_attempted = True
        admit_invocation(ctx, unit='turn')
        self.assertEqual((ctx.request_count, ctx.wrap_up_requests), (1, 1))

    def test_engine_retries_non_pi_deadline_yield_with_reserved_attempt(self):
        class AdmissionDriver:
            requires_sandbox = False

            def __init__(self): self.attempts = []

            def execute(self, context, prompt, *, profile=None):
                context.handle('_hello', {})
                if not self.attempts:
                    context.wrap_up_at = time.monotonic() - 1
                try:
                    admit_invocation(context, unit='query')
                except DeadlineWrapUp:
                    self.attempts.append((context.wrap_up_attempted, context.request_count,
                                          context.wrap_up_requests))
                    raise
                self.attempts.append((context.wrap_up_attempted, context.request_count,
                                      context.wrap_up_requests))
                context.end_request(0)
                context.handle('finish', {'outcome':'wait', 'summary':'Bounded result',
                                          'state':'Next: continue later.'})
                return {'requests':context.request_count}

        ctx = self.context('worker')
        driver = AdmissionDriver()
        result = Engine(self.config, driver=driver)._execute(ctx, driver, 'original prompt')
        self.assertEqual(driver.attempts, [(False, 0, 0), (True, 1, 1)])
        self.assertEqual(ctx.finished['outcome'], 'bounded')
        self.assertEqual(result['requests'], 1)

    def test_both_drivers_return_normal_unsealed_terminal_turns(self):
        for engine,unit in (('codex','turn'),('claude','query')):
            ctx,result=self.execute(engine)
            self.assertEqual(result['request_unit'],unit)
            self.assertEqual(result['requests'],1)
            self.assertTrue(result['usage_known'])
            self.assertIsNotNone(ctx.finished)
            self.assertTrue((ctx.run_dir/(engine+'-events.jsonl')).is_file())
            missing,result=self.execute(engine,'missing-finish')
            self.assertIsNone(missing.finished)
            self.assertEqual(result['request_unit'],unit)
            self.assertEqual(missing.request_deadlines, {})

    def test_checkpoint_is_inert_and_claim_is_finish_only(self):
        for engine, unit in (('codex', 'turn'), ('claude', 'query')):
            ctx, _ = self.execute(engine, 'missing-finish', context=self.context('worker'))
            checkpoint = ctx.driver_checkpoint
            self.assertEqual((checkpoint.engine, checkpoint.unit), (engine, unit))
            self.assertEqual((checkpoint.execution_attempt, checkpoint.requests), (0, 1))
            self.assertFalse(ctx.checkpoint_seal_only)
            self.assertIsNone(ctx.finished)
            self.assertFalse((ctx.run_dir/'finish-intent.json').exists())
            with self.assertRaises(Denied):
                ctx.claim_driver_checkpoint(dataclasses.replace(checkpoint))
            ctx.claim_driver_checkpoint(checkpoint)
            with self.assertRaises(Denied):
                ctx.claim_driver_checkpoint(checkpoint)
            # Even reopening the normal transport cannot reopen ordinary work.
            ctx.operations_stopped.clear()
            for operation, arguments in (('files', {}), ('_budget', {'sequence': 1}),
                                         ('_engine_tool', {'name': 'x', 'invocation_id': 'x'})):
                with self.assertRaisesRegex(Denied, 'Only finish'):
                    ctx.handle(operation, arguments)
            with self.assertRaisesRegex(ProtocolError, 'checkpoint seal'):
                admit_invocation(ctx, unit=unit)
            for driver in (PiDriver(ctx.config), CodexDriver(ctx.config), ClaudeDriver(ctx.config)):
                with self.assertRaisesRegex(Denied, 'checkpoint seal'):
                    driver.execute(ctx, 'must not launch')
            with self.assertRaisesRegex(Denied, 'one admitted model unit'):
                ctx.handle('finish', {'outcome': 'wait', 'summary': 'not a model-driven seal'})
            self.assertIsNone(ctx.finished)

    def seal_context(self, engine, scenario='seal-wait', *, role='worker'):
        ctx, _ = self.execute(engine, scenario, persistent=True, context=self.context(role))
        return ctx, Engine(ctx.config)

    def test_checkpoint_seal_is_one_real_driver_unit_with_finish_only_grant(self):
        for engine, unit in (('codex', 'turn'), ('claude', 'query')):
            for outcome in ('wait', 'done', 'blocked', 'continue'):
                with self.subTest(engine=engine, outcome=outcome):
                    ctx, host = self.seal_context(engine, 'seal-'+outcome)
                    role, snapshot = ctx.role, self.project.snapshots.get()['id']
                    content = (ctx.workspace/'app.py').read_bytes()
                    token = ctx.driver_checkpoint
                    if outcome == 'done':
                        ctx.verification = {'passed': True}
                    result = host._seal_driver_checkpoint(ctx, token)
                    self.assertEqual(result['request_unit'], unit)
                    self.assertEqual(ctx.request_count, 2)
                    self.assertEqual(ctx.execution_attempt, 1)
                    self.assertEqual(ctx.finished['outcome'], outcome)
                    self.assertEqual(ctx.finished['state'], 'Retain state')
                    self.assertEqual(ctx.role, role)
                    self.assertTrue(ctx.operations_stopped.is_set())
                    self.assertEqual(ctx.request_deadlines, {})
                    self.assertEqual(ctx.runtime_usage[(1, 1)], {'outputTokens': 5})
                    self.assertIsNone(ctx.driver_checkpoint)
                    self.assertEqual(self.project.snapshots.get()['id'], snapshot)
                    self.assertEqual((ctx.workspace/'app.py').read_bytes(), content)
                    self.assertFalse((ctx.run_dir/'continuation-intent.json').exists())
                    self.assertIsNone(ctx.missing_finish_evidence)
                    with self.assertRaises(Denied):
                        host._seal_driver_checkpoint(ctx, token)
                    with self.assertRaises(Denied):
                        driver_for(ctx.config, ctx.role.profile, {}).execute(ctx, 'no more work')
                    with self.assertRaises(ProtocolError):
                        admit_invocation(ctx, unit=unit)
                    self.assertEqual(ctx.request_count, 2)

    def test_checkpoint_seal_missing_failed_and_unsettled_are_not_retried(self):
        for engine in ('codex', 'claude'):
            for scenario in ('seal-missing', 'seal-failed', 'seal-provider-error', 'seal-wait'):
                with self.subTest(engine=engine, scenario=scenario):
                    ctx, host = self.seal_context(engine, scenario)
                    original = ctx.dispatch
                    calls = []
                    def dispatch(op, args):
                        if op == 'finish':
                            calls.append(args)
                            if scenario == 'seal-failed':
                                raise OSError('seal storage failure')
                        return original(op, args)
                    ctx.dispatch = dispatch
                    with unittest.mock.patch.object(Channel, 'quiescent', scenario != 'seal-wait'):
                        with self.assertRaises((ProtocolError, ModelFailure, OSError)):
                            host._seal_driver_checkpoint(ctx, ctx.driver_checkpoint)
                    self.assertEqual(ctx.request_count, 2)
                    self.assertEqual(len(calls), 1 if scenario in ('seal-failed', 'seal-wait') else 0)
                    self.assertEqual(ctx.finished is not None, scenario == 'seal-wait')
                    self.assertTrue(ctx.checkpoint_seal_only)
                    self.assertTrue(ctx.operations_stopped.is_set())
                    self.assertFalse(ctx._checkpoint_launch_pending)
                    self.assertFalse(ctx._checkpoint_driver_started)
                    with self.assertRaises((Denied, Cancelled)):
                        ctx.handle('finish', {'outcome': 'continue', 'summary': 'no retry'})
                    with self.assertRaises(Denied):
                        driver_for(ctx.config, ctx.role.profile, {}).execute(ctx, 'no retry')

    def test_checkpoint_seal_checks_stop_authority_deadline_and_budget_without_retry(self):
        for engine in ('codex', 'claude'):
            for guard in ('stop', 'paused', 'grant', 'engine-tools', 'wall', 'deadline', 'daily', 'requests', 'tools'):
                with self.subTest(engine=engine, guard=guard):
                    ctx, host = self.seal_context(engine)
                    token = ctx.driver_checkpoint
                    expected = Denied
                    if guard == 'stop':
                        ctx.stop.set()
                        expected = Cancelled
                    elif guard == 'paused':
                        self.project.set_control(paused=True)
                        expected = Cancelled
                    elif guard == 'grant':
                        ctx.role = dataclasses.replace(ctx.role, capabilities=())
                    elif guard == 'engine-tools':
                        ctx.role = dataclasses.replace(ctx.role, engine_tools=('x',))
                    elif guard == 'wall':
                        ctx.deadline = time.monotonic()-1
                        expected = RunDeadlineExceeded
                    elif guard == 'deadline':
                        ctx.wrap_up_at = time.monotonic()-1
                        expected = DeadlineWrapUp
                    elif guard == 'requests':
                        ctx.config = dataclasses.replace(ctx.config, limits=dataclasses.replace(
                            ctx.config.limits, requests_per_run=1, wrap_up_requests=0))
                        expected = LimitExceeded
                    elif guard == 'tools':
                        ctx.config = dataclasses.replace(ctx.config, limits=dataclasses.replace(
                            ctx.config.limits, tools_per_run=0, wrap_up_tools=0))
                        expected = LimitExceeded
                    patcher = (unittest.mock.patch('mizu.drivers.Budget.take', side_effect=InfraExceeded('daily cap'))
                               if guard == 'daily' else nullcontext())
                    if guard == 'daily':
                        expected = InfraExceeded
                    try:
                        with patcher, self.assertRaises(expected):
                            host._seal_driver_checkpoint(ctx, token)
                        self.assertIsNone(ctx.finished)
                        self.assertEqual(ctx.request_count, 2 if guard == 'tools' else 1)
                    finally:
                        self.project.set_control(paused=False)

    def test_checkpoint_is_writer_only_and_ephemeral_runs_cannot_claim(self):
        for engine in ('codex', 'claude'):
            reader, host = self.seal_context(engine, 'missing-finish', role='consult')
            self.assertIsNone(reader.driver_checkpoint)
            with self.assertRaises(Denied):
                host._seal_driver_checkpoint(reader, reader.driver_checkpoint)
            writer, _ = self.execute(engine, 'missing-finish', context=self.context('worker'))
            token = writer.driver_checkpoint
            writer.ephemeral = True
            with self.assertRaises(Denied):
                writer.claim_driver_checkpoint(token)
            self.assertFalse(writer.checkpoint_seal_only)

    def test_checkpoint_seal_unsupported_engines_never_launch(self):
        for engine in ('pi', 'pi-durable'):
            ctx = self.context('consult')
            profile = ctx.role.profile
            ctx.config = dataclasses.replace(ctx.config, profiles={**ctx.config.profiles, profile:
                {**ctx.config.profiles[profile], 'engine': engine}})
            token = DriverCheckpoint(engine, 'model_request', 0, 1)
            with unittest.mock.patch('mizu.runtime.driver_for') as launch:
                with self.assertRaises(Denied):
                    Engine(ctx.config)._seal_driver_checkpoint(ctx, token)
            launch.assert_not_called()
            self.assertEqual(ctx.request_count, 0)
            self.assertFalse(ctx.checkpoint_seal_only)

    def test_checkpoint_seal_preserves_existing_wrap_up_and_writable_finish_checks(self):
        for engine in ('codex', 'claude'):
            ctx, host = self.seal_context(engine)
            ctx.config = dataclasses.replace(ctx.config, limits=dataclasses.replace(
                ctx.config.limits, requests_per_run=1, wrap_up_requests=1, tools_per_run=0, wrap_up_tools=1))
            host._seal_driver_checkpoint(ctx, ctx.driver_checkpoint)
            self.assertEqual(ctx.wrap_up_requests, 1)
            self.assertEqual(ctx.wrap_up_tools, 1)
            self.assertEqual(ctx.finished['outcome'], 'bounded')
            self.assertEqual(ctx.finished['bound'], 'requests')
            ctx, host = self.seal_context(engine, 'seal-done', role='worker')
            with self.assertRaisesRegex(Denied, 'acceptance commands'):
                host._seal_driver_checkpoint(ctx, ctx.driver_checkpoint)
            self.assertIsNone(ctx.finished)
            self.assertEqual(ctx.request_count, 2)
            self.assertFalse((ctx.run_dir/'finish-intent.json').exists())

    def test_checkpoint_seal_stop_and_authority_win_at_inflight_boundaries(self):
        for engine in ('codex', 'claude'):
            for phase in ('startup', 'terminal'):
                for guard in ('stop', 'authority', 'wall'):
                    with self.subTest(engine=engine, phase=phase, guard=guard):
                        ctx, host = self.seal_context(engine)
                        original = Channel.receive
                        changed = False
                        def receive(channel, *args, **kwargs):
                            nonlocal changed
                            event = original(channel, *args, **kwargs)
                            terminal = event.get('type') == 'result' or event.get('method') == 'turn/completed'
                            if not changed and (phase == 'startup' or terminal):
                                changed = True
                                if guard == 'stop':
                                    ctx.stop.set()
                                elif guard == 'authority':
                                    self.project.set_control(armed=False)
                                else:
                                    ctx.deadline = time.monotonic()-1
                            return event
                        try:
                            with unittest.mock.patch.object(Channel, 'receive', receive):
                                with self.assertRaises(RunDeadlineExceeded if guard == 'wall' else Cancelled):
                                    host._seal_driver_checkpoint(ctx, ctx.driver_checkpoint)
                            self.assertTrue(changed)
                            self.assertEqual(ctx.request_count, 1 if phase == 'startup' else 2)
                            self.assertEqual(ctx.finished is not None, phase == 'terminal')
                            self.assertTrue(ctx.operations_stopped.is_set())
                        finally:
                            self.project.set_control(armed=True)

    def test_checkpoint_launch_is_bound_to_its_engine_before_launch(self):
        for engine, unit in (('codex', 'turn'), ('claude', 'query')):
            ctx, _ = self.execute(engine, 'missing-finish', context=self.context('worker'))
            token = ctx.driver_checkpoint
            ctx.claim_driver_checkpoint(token)
            ctx._checkpoint_launch_pending = True
            with self.assertRaises(Denied):
                PiDriver(ctx.config).execute(ctx, 'wrong adapter')
            self.assertTrue(ctx._checkpoint_launch_pending)
            wrong_engine, wrong_unit = (('claude', 'query') if engine == 'codex' else ('codex', 'turn'))
            with self.assertRaises(Denied):
                ctx.begin_driver(wrong_engine, wrong_unit)
            self.assertTrue(ctx._checkpoint_launch_pending)
            ctx.begin_driver(engine, unit)
            self.assertFalse(ctx._checkpoint_launch_pending)
            self.assertTrue(ctx._checkpoint_driver_started)

    def test_checkpoint_seal_barriers_fence_launch_admission_and_post_finish(self):
        for engine in ('codex', 'claude'):
            for phase in ('startup', 'provider', 'sealed', 'teardown'):
                with self.subTest(engine=engine, phase=phase):
                    ctx, host = self.seal_context(engine)
                    entered, release = threading.Event(), threading.Event()
                    errors = []
                    original_receive, original_close = Channel.receive, Channel.close
                    def barrier():
                        entered.set()
                        if not release.wait(60):
                            raise AssertionError('seal phase barrier timed out')
                    def receive(channel, *args, **kwargs):
                        if phase == 'startup' and not entered.is_set():
                            barrier()
                        event = original_receive(channel, *args, **kwargs)
                        if phase == 'provider' and (event.get('type') == 'result' or event.get('method') == 'turn/completed'):
                            barrier()
                        return event
                    def close(channel):
                        if phase == 'teardown':
                            barrier()
                        return original_close(channel)
                    original_dispatch = ctx.dispatch
                    def dispatch(op, args):
                        result = original_dispatch(op, args)
                        if phase == 'sealed' and op == 'finish':
                            barrier()
                        return result
                    ctx.dispatch = dispatch
                    def run():
                        try:
                            host._seal_driver_checkpoint(ctx, ctx.driver_checkpoint)
                        except Exception as exc:
                            errors.append(exc)
                    with unittest.mock.patch.object(Channel, 'receive', receive), unittest.mock.patch.object(Channel, 'close', close):
                        thread = threading.Thread(target=run)
                        thread.start()
                        try:
                            self.assertTrue(entered.wait(60))
                            self.assertTrue(ctx.checkpoint_seal_only)
                            self.assertFalse(ctx._checkpoint_launch_pending)
                            self.assertIsNone(ctx.driver_checkpoint)
                            # The sealed barrier holds Context's operation mutex:
                            # no other host dispatch can pass it until it settles.
                            if phase != 'sealed':
                                with self.assertRaises(Denied):
                                    ctx.begin_driver('claude', 'query')
                                with self.assertRaises(ProtocolError):
                                    admit_invocation(ctx, unit='turn' if engine == 'codex' else 'query')
                                with self.assertRaises(Denied):
                                    ctx.handle('files', {})
                            if phase in ('provider', 'sealed', 'teardown'):
                                self.assertIsNotNone(ctx.finished)
                        finally:
                            release.set()
                            thread.join(60)
                        self.assertFalse(thread.is_alive())
                        self.assertEqual(errors, [])
                    self.assertTrue(ctx._checkpoint_settled)

    def test_checkpoint_fails_closed_for_stale_active_and_unproven_paths(self):
        ctx, _ = self.execute('claude', 'missing-finish', context=self.context('worker'))
        checkpoint = ctx.driver_checkpoint
        for mutate, restore in (
                (lambda: ctx.request_deadlines.update({(0, 1): time.monotonic()+60}), ctx.request_deadlines.clear),
                (ctx.operation_active.set, ctx.operation_active.clear),
                (ctx.joins_idle.clear, ctx.joins_idle.set),
                (ctx.operations_stopped.clear, ctx.operations_stopped.set),
                (lambda: setattr(ctx, 'execution_attempt', 1), lambda: setattr(ctx, 'execution_attempt', 0)),
                (lambda: setattr(ctx, 'request_count', 2), lambda: setattr(ctx, 'request_count', 1)),
                (lambda: setattr(ctx, 'request_failure_pending', True), lambda: setattr(ctx, 'request_failure_pending', False)),
                (lambda: setattr(ctx, 'admission_error', 'request_ceiling'), lambda: setattr(ctx, 'admission_error', None)),
                (lambda: setattr(ctx, 'admission_exception', LimitExceeded('stale admission')), lambda: setattr(ctx, 'admission_exception', None)),
                (lambda: setattr(ctx, 'bound_hit', 'requests'), lambda: setattr(ctx, 'bound_hit', None))):
            mutate()
            try:
                with self.assertRaises(Denied):
                    ctx.claim_driver_checkpoint(checkpoint)
                self.assertFalse(ctx.checkpoint_seal_only)
            finally:
                restore()
        ctx.stop.set()
        with self.assertRaises(Cancelled):
            ctx.claim_driver_checkpoint(checkpoint)
        ctx.stop.clear()
        ctx.begin_driver('claude', 'query')
        with self.assertRaises(Denied):
            ctx.claim_driver_checkpoint(checkpoint)
        for engine, unit in (('pi', 'model_request'), ('pi-durable', 'model_request')):
            ctx.complete_driver_checkpoint(engine, unit)
            self.assertIsNone(ctx.driver_checkpoint)
        ctx.role = dataclasses.replace(ctx.role, engine_tools=('mcp__synthetic__search',))
        ctx.complete_driver_checkpoint('claude', 'query')
        self.assertIsNone(ctx.driver_checkpoint)
        # Real driver's success with an engine-tool grant also stays unsupported.
        ctx.operations_stopped.clear()
        self.execute('claude', 'missing-finish', context=ctx)
        self.assertIsNone(ctx.driver_checkpoint)
        for engine, scenario in (('codex', 'failed'), ('claude', 'api-error')):
            failed = self.context('consult')
            with self.assertRaises((ModelFailure, ProtocolError)):
                self.execute(engine, scenario, context=failed)
            self.assertIsNone(failed.driver_checkpoint)

    def test_channel_quiescence_requires_exited_process_and_stopped_readers(self):
        channel = object.__new__(Channel)
        channel.stop = threading.Event()
        channel.process = unittest.mock.Mock()
        reader = unittest.mock.Mock()
        channel.readers = [reader]
        channel.process.poll.return_value = None
        reader.is_alive.return_value = True
        self.assertFalse(channel.quiescent)
        channel.stop.set()
        self.assertFalse(channel.quiescent)
        channel.process.poll.return_value = 0
        self.assertFalse(channel.quiescent)
        reader.is_alive.return_value = False
        self.assertTrue(channel.quiescent)

    def test_checkpoint_requires_proven_transport_teardown(self):
        for engine in ('codex', 'claude'):
            with unittest.mock.patch.object(Channel, 'quiescent', False):
                ctx, _ = self.execute(engine, 'missing-finish', context=self.context('worker'))
            self.assertIsNone(ctx.driver_checkpoint)

    def test_checkpoint_waits_for_provider_tool_join_and_teardown(self):
        # Actual Python drivers and authenticated Bridge, synthetic protocol
        # peers (not live providers). Events, not sleeps, control completion.
        for engine in ('codex', 'claude'):
            for phase in ('provider', 'tool', 'join', 'teardown'):
                with self.subTest(engine=engine, phase=phase):
                    ctx = self.context('worker')
                    ctx.role = dataclasses.replace(ctx.role, capabilities=tuple(dict.fromkeys((*ctx.role.capabilities, 'consult'))))
                    entered, release = threading.Event(), threading.Event()
                    errors = []
                    def barrier():
                        entered.set()
                        if not release.wait(60):
                            raise AssertionError('checkpoint test release timed out')
                    def child(_ctx, _args):
                        barrier()
                        return {'summary': 'settled child'}
                    ctx.consult = child
                    original_dispatch = ctx.dispatch
                    def dispatch(operation, args):
                        if phase == 'tool' and operation == 'files':
                            barrier()
                        return original_dispatch(operation, args)
                    ctx.dispatch = dispatch
                    original_receive = Channel.receive
                    def receive(channel, *args, **kwargs):
                        event = original_receive(channel, *args, **kwargs)
                        if phase == 'provider' and (event.get('type') == 'result' or event.get('method') == 'turn/completed'):
                            barrier()
                        return event
                    original_exit = Bridge.__exit__
                    def bridge_exit(bridge, *args):
                        if phase == 'teardown':
                            barrier()
                        return original_exit(bridge, *args)
                    def run():
                        try:
                            self.execute(engine, 'checkpoint-'+phase if phase in ('tool', 'join') else 'missing-finish', context=ctx)
                        except BaseException as exc:
                            errors.append(exc)
                    with unittest.mock.patch.object(Channel, 'receive', receive), \
                         unittest.mock.patch.object(Bridge, '__exit__', bridge_exit):
                        thread = threading.Thread(target=run)
                        thread.start()
                        try:
                            self.assertTrue(entered.wait(60), 'checkpoint boundary not reached')
                            self.assertIsNone(ctx.driver_checkpoint)
                            self.assertFalse(ctx.checkpoint_seal_only)
                            if phase in ('provider', 'tool', 'join'):
                                self.assertTrue(ctx.request_deadlines)
                            if phase in ('tool', 'join'):
                                self.assertTrue(ctx.operation_active.is_set())
                            if phase == 'join':
                                self.assertFalse(ctx.joins_idle.is_set())
                        finally:
                            release.set()
                            thread.join(60)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(errors, [])
                    self.assertIsNotNone(ctx.driver_checkpoint)
                    self.assertEqual(ctx.request_deadlines, {})
                    self.assertFalse(ctx.operation_active.is_set())
                    self.assertTrue(ctx.joins_idle.is_set())
                    ctx.claim_driver_checkpoint(ctx.driver_checkpoint)

    def test_host_relative_bound_is_only_for_initial_engine_startup(self):
        original_receive = Channel.receive
        for engine in ('claude', 'codex'):
            with self.subTest(engine=engine):
                observed = []
                def capture(channel, *args, **kwargs):
                    until = kwargs.get('until')
                    if until is not None:
                        observed.append((kwargs.get('bound'), until - time.monotonic()))
                    return original_receive(channel, *args, **kwargs)
                with unittest.mock.patch(f'mizu.{engine}.startup_bound', return_value=60), \
                     unittest.mock.patch.object(Channel, 'receive', new=capture):
                    self.execute(engine)
                self.assertTrue(observed)
                # The first protocol response uses the measured 60 s host bound.
                self.assertEqual(observed[0][0], 60)
                self.assertGreater(observed[0][1], 0)
                self.assertLessEqual(observed[0][1], 60)
                if engine == 'claude':
                    self.assertEqual(len(observed), 1)
                else:
                    # Codex's subsequent app-server RPCs keep their existing
                    # 30 s cap instead of inheriting the startup bound.
                    self.assertGreater(len(observed), 1)
                    for bound, remaining in observed[1:]:
                        self.assertIsNone(bound)
                        self.assertGreater(remaining, 0)
                        # The 30 s cap is measured from request start; allow a
                        # small platform-timer/floating-point margin so the
                        # assertion does not depend on host clock resolution.
                        self.assertLessEqual(remaining, 31)

    def test_answered_startup_refusals_are_recorded_before_classification(self):
        for engine, scenario in (('claude', 'ready-error'), ('codex', 'initialize-error')):
            with self.subTest(engine=engine):
                with unittest.mock.patch(f'mizu.{engine}.startup_bound', return_value=60), \
                     unittest.mock.patch(f'mizu.{engine}.record_startup') as record:
                    with self.assertRaises(ProtocolError):
                        self.execute(engine, scenario)
                record.assert_called_once()
                self.assertEqual(record.call_args.args[1], engine)
                # The runtime's interval is a spawn-to-first-answer
                # measurement, so the record names that endpoint.
                self.assertEqual(record.call_args.args[3], 'runtime')

    def test_pi_records_a_refused_first_state_answer(self):
        ctx = self.context('consult')
        profile = ctx.role.profile
        raw = {**self.config.profiles[profile], 'engine': 'pi', 'provider': 'custom',
               'options': {}, 'session': 'ephemeral'}
        engines = {**self.config.engines, 'pi': {**self.config.engines['pi'],
            'command': (sys.executable, str(ROOT/'tests/fake_pi.py'),
                        '--fake-scenario', 'refused-state')}}
        config = dataclasses.replace(self.config, profiles={**self.config.profiles, profile: raw},
                                     engines=engines)
        ctx.config = config
        with unittest.mock.patch('mizu.pi.startup_bound', return_value=60), \
             unittest.mock.patch('mizu.pi.record_startup') as record:
            with self.assertRaisesRegex(ProtocolError, 'handshake failed'):
                PiDriver(config).execute(ctx, 'synthetic prompt')
        record.assert_called_once()
        self.assertEqual(record.call_args.args[1], 'pi')
        # The runtime's interval is a spawn-to-first-answer measurement,
        # so the record names that endpoint.
        self.assertEqual(record.call_args.args[3], 'runtime')

    def test_a_startup_bound_refusal_names_the_bound_it_was_held_to(self):
        # A child that never answers its initial handshake is refused by the
        # bound it was held to, so the refusal names the bound rather than
        # the engine. Its silence contributes no start-up sample: only an
        # answer is a measurement.
        for engine, scenario in (('claude', 'no-ready'), ('codex', 'no-initialize')):
            with self.subTest(engine=engine):
                with unittest.mock.patch(f'mizu.{engine}.startup_bound', return_value=2), \
                     unittest.mock.patch(f'mizu.{engine}.record_startup') as record:
                    with self.assertRaisesRegex(ProtocolError, 'exceeded its 2 s bound'):
                        self.execute(engine, scenario)
                record.assert_not_called()

    def test_pi_startup_bound_refusal_names_the_bound_it_was_held_to(self):
        ctx = self.context('consult')
        profile = ctx.role.profile
        raw = {**self.config.profiles[profile], 'engine': 'pi', 'provider': 'custom',
               'options': {}, 'session': 'ephemeral'}
        engines = {**self.config.engines, 'pi': {**self.config.engines['pi'],
            'command': (sys.executable, str(ROOT/'tests/fake_pi.py'),
                        '--fake-scenario', 'silent-state')}}
        config = dataclasses.replace(self.config, profiles={**self.config.profiles, profile: raw},
                                     engines=engines)
        ctx.config = config
        with unittest.mock.patch('mizu.pi.startup_bound', return_value=2), \
             unittest.mock.patch('mizu.pi.record_startup') as record:
            with self.assertRaisesRegex(ProtocolError, 'exceeded its 2 s bound'):
                PiDriver(config).execute(ctx, 'synthetic prompt')
        record.assert_not_called()

    def test_the_durable_runtime_keeps_its_transport_deadline_path(self):
        # ADR-042: the durable runtime keeps its own transport-deadline path
        # and does not consume the managed start-up record. The record holds
        # a spawn-to-first-answer interval, and the durable child answers
        # with a whole submission, so its interval is not a start-up
        # measurement; its deadline stays the transport deadline.
        import inspect
        import types
        from mizu import pi_durable
        from mizu.startup import record_path
        source = inspect.getsource(pi_durable)
        for name in ('startup_bound', 'record_startup', 'adapter-startup'):
            self.assertFalse(name in source,
                             'the durable runtime must not use the managed start-up record: '+name)
        profile = 'durable'
        self.config.profiles[profile] = {**self.config.profiles['primary'], 'engine': 'pi-durable'}
        ctx = self.context('worker')
        ctx.handle('_hello', {})
        ctx.handle('_budget', {'sequence': 1})
        ctx.handle('finish', {'outcome': 'wait', 'summary': 'One observed result',
                              'state': 'Next step is explicit.'})
        settings = effective(self.config, ctx.role, profile)
        model = self.config.model(profile)
        reported = {'durable_result': True, 'status': 'done', 'finish_called': True,
                    'requests': 1, 'usage': {'input': 1, 'output': 1}}
        fake = types.SimpleNamespace(reason='exited', exit_code=0,
                                     stdout=json.dumps(reported), stderr='')
        seen = {}

        def fake_run(*args, timeout=None, **kwargs):
            seen['timeout'] = timeout
            return fake

        with unittest.mock.patch.object(pi_durable, 'Bridge') as bridge_cls, \
             unittest.mock.patch.object(pi_durable, 'run', side_effect=fake_run):
            bridge_cls.return_value.__enter__.return_value.config_file = str(ctx.run_dir/'bridge.json')
            pi_durable.PiDurableDriver(self.config)._run_once(
                ctx, settings, model, 'hello', profile, 'run-key', Path('/tmp/store.sqlite'))
        # The durable child is held to the transport deadline, and the
        # shared start-up record is not written by it.
        self.assertTrue(callable(seen.get('timeout')))
        self.assertFalse(record_path(self.config).exists())

    def test_claude_and_codex_resume_a_work_capable_no_tool_followup(self):
        from mizu.runtime import Engine
        for engine in ('claude', 'codex'):
            for session in ('persistent', 'ephemeral'):
                with self.subTest(engine=engine, session=session):
                    ctx=self.context('consult')
                    profile=ctx.role.profile
                    raw={**self.config.profiles[profile], 'engine':engine, 'provider':'custom',
                         'options':{}, 'session':session}
                    engines={**self.config.engines,engine:{**self.config.engines[engine],
                        'command':(sys.executable,str(ROOT/'tests/fake_engines.py'),engine,'finish-on-resume')}}
                    config=dataclasses.replace(self.config,profiles={**self.config.profiles,profile:raw},engines=engines)
                    ctx.config=config
                    inner=CodexDriver(config) if engine=='codex' else ClaudeDriver(config)
                    class RecordingDriver:
                        requires_sandbox=False
                        def __init__(self):
                            self.grants=[]
                            self.calls=0
                        def execute(self, context, prompt, *, profile=None):
                            self.calls += 1
                            self.grants.append(context.role.capabilities)
                            return inner.execute(context,prompt,profile=profile)
                    driver=RecordingDriver()
                    original_grant = ctx.role.capabilities
                    with unittest.mock.patch.dict(os.environ,{'CODEX_AUTH_FILE':str(self.root/'absent')}):
                        model=Engine(config,driver=driver)._execute(ctx,driver,'Synthetic work')
                    self.assertIsNotNone(ctx.finished)
                    self.assertEqual(driver.calls,2)
                    self.assertEqual(driver.grants[1], original_grant)
                    self.assertEqual(ctx.request_count,2)
                    self.assertEqual(ctx.request_deadlines,{})
                    self.assertEqual(model['missing_finish']['used_followups'],1)

    def test_pi_resumes_a_normal_unsealed_turn_with_work_capable_followup(self):
        from mizu.runtime import Engine
        for session in ('persistent', 'ephemeral'):
            with self.subTest(session=session):
                ctx=self.context('consult')
                profile=ctx.role.profile
                raw={**self.config.profiles[profile], 'engine':'pi', 'provider':'custom',
                     'options':{}, 'session':session}
                engines={**self.config.engines,'pi':{**self.config.engines['pi'],
                    'command':(sys.executable,str(ROOT/'tests/fake_pi.py'),
                               '--fake-scenario','finish-on-resume')}}
                config=dataclasses.replace(self.config,profiles={**self.config.profiles,profile:raw},engines=engines)
                ctx.config=config
                inner=PiDriver(config)
                class RecordingDriver:
                    requires_sandbox=False
                    def __init__(self):
                        self.grants=[]
                        self.calls=0
                    def execute(self, context, prompt, *, profile=None):
                        self.calls += 1
                        self.grants.append(context.role.capabilities)
                        return inner.execute(context,prompt,profile=profile)
                driver=RecordingDriver()
                original_grant = ctx.role.capabilities
                model=Engine(config,driver=driver)._execute(ctx,driver,'Synthetic work')
                self.assertIsNotNone(ctx.finished)
                self.assertEqual(driver.calls,2)
                self.assertEqual(driver.grants[1], original_grant)
                self.assertEqual(ctx.request_count,2)
                self.assertEqual(ctx.request_deadlines,{})
                self.assertEqual(model['missing_finish']['used_followups'],1)

    def test_codex_terminal_failure_and_unanswerable_request(self):
        for scenario in ('failed','interrupted','approval','mcp-failure','provider-error-before-finish','bad-json','exit'):
            with self.subTest(scenario=scenario), self.assertRaises(ProtocolError): self.execute('codex',scenario)

    def test_claude_success_subtype_does_not_override_api_error(self):
        with self.assertRaises(ProtocolError): self.execute('claude','api-error')
        for reason in ('max_turns','max_budget_usd','aborted_tools',None):
            with self.assertRaises(ProtocolError): terminal_result({'subtype':'success','terminal_reason':reason,'is_error':False})

    def test_selector_routes_between_existing_engines(self):
        from mizu.runtime import Engine
        from mizu.selection import validate_selectors
        engines = dict(self.config.engines)
        profiles = dict(self.config.profiles)
        for profile, engine, scenario in (('primary', 'codex', 'provider-error-before-finish'), ('alternate', 'claude', 'normal')):
            profiles[profile] = {**profiles[profile], 'engine': engine, 'provider': 'synthetic', 'options': {}, 'session': 'ephemeral'}
            engines[engine] = {**engines[engine], 'command': (sys.executable, str(ROOT/'tests/fake_engines.py'), engine, scenario)}
        spec = {'retry_seconds': 10, 'rules': [{'candidates': [{'profile': 'primary'}, {'profile': 'alternate'}]}],
                'on_error': [{'when': {'path': 'error.code', 'op': 'eq', 'value': 'synthetic-code'}, 'scope': 'profile', 'seconds': 60}]}
        validate_selectors({'dynamic': spec}, profiles, self.file.parent)
        role = dataclasses.replace(self.config.roles['worker'], profile='', selector='dynamic', workspace='read', capabilities=('finish',))
        config = dataclasses.replace(self.config, profiles=profiles, engines=engines,
                                     roles={**self.config.roles, 'worker': role}, selectors={'dynamic': spec})
        with unittest.mock.patch.dict(os.environ, {'CODEX_AUTH_FILE': str(self.root/'absent')}):
            first = Engine(config).run(self.project, 'worker')
            second = Engine(config).run(self.project, 'worker')
        # The matched profile block falls back within the first dispatch:
        # codex fails before sealing, claude completes it, and no second run is needed.
        self.assertEqual(first['status'], 'completed')
        self.assertEqual(first['model']['engine'], 'claude')
        self.assertEqual(first['selection']['profile'], 'alternate')
        self.assertEqual(second['status'], 'completed')
        self.assertEqual(second['model']['engine'], 'claude')
        self.assertEqual(second['selection']['profile'], 'alternate')
        self.assertFalse(self.project.control()['paused'])

    def test_runtime_keeps_seal_after_synthetic_codex_terminal_failure(self):
        from mizu.runtime import Engine
        profile = {**self.config.profiles['primary'], 'engine': 'codex', 'provider': 'synthetic',
                   'options': {}, 'session': 'ephemeral'}
        engine = {**self.config.engines['codex'],
                  'command': (sys.executable, str(ROOT/'tests/fake_engines.py'), 'codex', 'provider-error')}
        role = dataclasses.replace(self.config.roles['worker'], workspace='read', capabilities=('finish',))
        config = dataclasses.replace(self.config, profiles={**self.config.profiles, 'primary': profile},
            engines={**self.config.engines, 'codex': engine}, roles={**self.config.roles, 'worker': role},
            limits=dataclasses.replace(self.config.limits, max_failures=1))
        with unittest.mock.patch.dict(os.environ, {'CODEX_AUTH_FILE': str(self.root/'absent')}):
            result = Engine(config).run(self.project, 'worker')
        run = self.project.root/'runs'/result['run']
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['finish'], read_json(run/'finish-intent.json'))
        self.assertEqual(result['finish']['summary'], 'Synthetic result')
        self.assertEqual(result['model']['engine'], 'codex')
        self.assertEqual(result['model']['requests'], 1)
        self.assertEqual(result['model']['request_unit'], 'turn')
        self.assertTrue(result['model']['usage_known'])
        self.assertFalse(result['model']['settled'])
        self.assertEqual(result['model']['settlement']['error_type'], 'ModelFailure')
        self.assertFalse((run/'error.json').exists())
        self.assertEqual(read_json(self.project.root/'health/worker.json')['consecutive_failures'], 0)
        self.assertFalse(self.project.control()['paused'])

    def test_native_failure_facts_are_preserved_without_guessing(self):
        for engine, scenario in (('codex', 'provider-error'), ('claude', 'query-error')):
            with self.subTest(engine=engine), self.assertRaises(ModelFailure) as caught:
                self.execute(engine, scenario)
            evidence = caught.exception.evidence
            self.assertEqual(evidence['source'], engine)
            self.assertEqual(evidence['code'], 'synthetic-code')
            if engine == 'codex':
                self.assertEqual(evidence['details']['codex_error_info']['synthetic']['httpStatusCode'], 503)
                self.assertIsNone(evidence['retry_at'])
            else:
                self.assertEqual(evidence['retry_at'], 2000000000)
        with self.assertRaises(ModelFailure) as caught:
            self.execute('codex', 'rpc-error')
        self.assertEqual(caught.exception.evidence['code'], -32000)
        with self.assertRaises(ModelFailure) as caught:
            self.execute('claude', 'api-error')
        self.assertEqual(caught.exception.evidence['kind'], 'api_error')
        for reason in ('max_turns', 'max_budget_usd', 'aborted_tools', 'cancelled'):
            with self.assertRaises(ProtocolError) as caught:
                terminal_result({'terminal_reason': reason})
            self.assertNotIsInstance(caught.exception, ModelFailure)

    def test_claude_api_error_status_is_preserved_as_selector_evidence(self):
        for status in (429, 503, 401):
            with self.subTest(status=status), self.assertRaises(ModelFailure) as caught:
                terminal_result({'terminal_reason': 'api_error', 'subtype': 'success',
                                 'is_error': True, 'errors': 'synthetic boom',
                                 'api_error_status': status})
            evidence = caught.exception.evidence
            self.assertEqual(evidence['kind'], 'api_error')
            self.assertEqual(evidence['code'], 'success')
            self.assertEqual(evidence['details']['api_error_status'], status)
            self.assertIsNone(evidence['retry_at'])
        with self.assertRaises(ModelFailure) as caught:
            terminal_result({'terminal_reason': 'api_error', 'subtype': 'success',
                             'is_error': True, 'errors': 'synthetic boom'})
        self.assertNotIn('api_error_status', caught.exception.evidence.get('details', {}))
        for invalid in (True, False, '429', 429.0, None, 99, 600, 0, -1, 1000, [], {}):
            with self.subTest(invalid=invalid), self.assertRaises(ModelFailure) as caught:
                terminal_result({'terminal_reason': 'api_error', 'subtype': 'success',
                                 'is_error': True, 'errors': 'synthetic boom',
                                 'api_error_status': invalid})
            self.assertNotIn('api_error_status', caught.exception.evidence.get('details', {}))

    def test_claude_api_error_status_driver_reaches_selector_without_policy_change(self):
        from mizu.selection import evaluate
        with self.assertRaises(ModelFailure) as caught:
            self.execute('claude', 'api-error')
        evidence = caught.exception.evidence
        self.assertEqual(evidence['kind'], 'api_error')
        self.assertEqual(evidence['code'], 'success')
        self.assertEqual(evidence['details']['api_error_status'], 429)
        self.assertIsNone(evidence['retry_at'])
        facts = {'error': evidence}
        self.assertTrue(evaluate({'path': 'error.details.api_error_status', 'op': 'eq', 'value': 429}, facts) is True)
        self.assertTrue(evaluate({'path': 'error.details.api_error_status', 'op': 'eq', 'value': 503}, facts) is False)
        self.assertTrue(evaluate({'path': 'error.code', 'op': 'eq', 'value': 'success'}, facts) is True)
        with self.assertRaises(ModelFailure) as absent:
            terminal_result({'terminal_reason': 'api_error', 'subtype': 'success',
                             'is_error': True, 'errors': 'synthetic boom'})
        self.assertIsNot(evaluate({'path': 'error.details.api_error_status', 'op': 'eq', 'value': 429},
                                  {'error': absent.exception.evidence}), True)
        with self.assertRaises(ModelFailure) as malformed:
            terminal_result({'terminal_reason': 'api_error', 'subtype': 'success',
                             'is_error': True, 'errors': 'synthetic boom',
                             'api_error_status': True})
        self.assertIsNot(evaluate({'path': 'error.details.api_error_status', 'op': 'eq', 'value': 1},
                                  {'error': malformed.exception.evidence}), True)
        with self.assertRaises(ModelFailure) as query:
            self.execute('claude', 'query-error')
        self.assertNotIn('api_error_status', query.exception.evidence.get('details', {}))

    def test_post_finish_calls_are_refused(self):
        for engine in ('codex','claude'):
            self.execute(engine,'post-finish')
        with self.assertRaises(Denied): self.execute('codex','native-after-finish')

    def test_new_and_resumed_system_context_contains_pinned_charter(self):
        # Synthetic peers exercise host serialization, not live inference or
        # provider compaction. Reconstruct Context for the resumed unit;
        # these read-only contexts must not use the newer live project goal.
        pinned_goal = "Pinned charter: café\r\nPreserve exact text.\r\n"
        snapshot = self.project.snapshots.create(
            self.project.snapshots.capture_files(self.project.workspace),
            goal=pinned_goal, state="", run=None, outcome="wait", summary="Pinned charter")
        self.project.snapshots.publish(snapshot)
        # Byte writes make CRLF policy coverage deterministic on every host.
        for policy in self.config.roles['consult'].policy:
            policy.write_bytes(b"Synthetic policy.\r\nKeep exact newlines.\r\n")
        (self.project.root / "PROJECT.md").write_text("New unpublished live charter")
        for engine in ('pi', 'codex', 'claude'):
            with self.subTest(engine=engine):
                expected = None
                for resumed in (False, True):
                    ctx = self.context('consult')
                    self.assertEqual(ctx.goal, pinned_goal)
                    if engine == 'pi':
                        profile = ctx.role.profile
                        raw = {**self.config.profiles[profile], 'engine': engine,
                               'provider': 'custom', 'options': {}, 'session': 'persistent'}
                        engines = {**self.config.engines, engine: {**self.config.engines[engine],
                            'command': (sys.executable, str(ROOT/'tests/fake_pi.py'))}}
                        config = dataclasses.replace(self.config,
                            profiles={**self.config.profiles, profile: raw}, engines=engines)
                        ctx.config = config
                        PiDriver(config).execute(ctx, 'digest-only unit')
                        cfg = read_json(ctx.run_dir/'pi-effective.json')
                        actual = cfg['systemPrompt']
                        self.assertEqual(bool(cfg['resume']), resumed)
                        self.assertEqual((ctx.run_dir/'system.md').read_bytes(), actual.encode('utf-8'))
                    else:
                        sent = []
                        send = Channel.send
                        def record(channel, message):
                            sent.append(message)
                            return send(channel, message)
                        with unittest.mock.patch.object(Channel, 'send', record):
                            self.execute(engine, persistent=True, context=ctx)
                        if engine == 'codex':
                            requests = [m for m in sent if m.get('method') in ('thread/start', 'thread/resume')]
                            self.assertEqual(len(requests), 1)
                            self.assertEqual(requests[0]['method'], 'thread/resume' if resumed else 'thread/start')
                            actual = requests[0]['params']['baseInstructions']
                        else:
                            cfg = read_json(ctx.run_dir/'claude-effective.json')
                            actual = cfg['systemPrompt']
                            self.assertEqual(bool(cfg['resume']), resumed)
                    self.assertEqual(actual, role_policy_text(ctx.role) + "\n# Project charter\n\n" + ctx.goal)
                    self.assertEqual(actual, system_context_text(ctx.role, ctx.goal))
                    self.assertNotIn("New unpublished live charter", actual)
                    if expected is not None:
                        self.assertEqual(actual, expected)
                    expected = actual

    def test_resume_uses_saved_session_and_usage_baseline(self):
        for engine in ('codex','claude'):
            first,result=self.execute(engine,persistent=True)
            second,resumed=self.execute(engine,persistent=True)
            self.assertEqual(result.get('conversation_id',result.get('session_id')),resumed.get('conversation_id',resumed.get('session_id')))
            self.assertTrue(resumed['usage_known'])
            values=resumed['usage'][0]
            self.assertEqual(values.get('input_tokens',values.get('inputTokens')),0)


class CumulativeUsageTests(unittest.TestCase):
    def test_codex_duplicates_and_reordering(self):
        usage=TokenUsage({'inputTokens':20})
        for count in (25,25,23,30): usage.observe({'inputTokens':count})
        self.assertEqual(usage.delta()['inputTokens'],10)
        for invalid in (-1,True,float('inf')):
            with self.assertRaises(ProtocolError): usage.observe({'inputTokens':invalid})

    def test_claude_model_totals_exclude_double_counted_children(self):
        value=model_delta({'main':{'inputTokens':30},'child':{'inputTokens':5}}, {'main':{'inputTokens':20}})
        self.assertEqual(sum(item['inputTokens'] for item in value),15)
        with self.assertRaises(ProtocolError): model_delta({'main':{'inputTokens':1}}, {'main':{'inputTokens':2}})
class McpProxyTests(unittest.TestCase):
    def bridge_config(self, handle):
        bridge = Bridge(handle, tool_definitions(("finish",)), timeout=60)
        bridge.__enter__()
        self.addCleanup(bridge.__exit__, None, None, None)
        return read_json(bridge.config_file)

    def transact(self, config, messages):
        source = io.BytesIO(b"".join(messages))
        sink = io.BytesIO()
        serve(input_stream=source, output_stream=sink, bridge=config)
        return [json.loads(line) for line in sink.getvalue().split(b"\n") if line]

    def test_tcp_loopback_forward_to_runtime(self):
        seen = []

        def handle(operation, arguments):
            seen.append((operation, arguments))
            return {"sealed": True}

        bridge = Bridge(handle, tool_definitions(("finish",)), timeout=60, transport="tcp")
        bridge.__enter__()
        self.addCleanup(bridge.__exit__, None, None, None)
        config = read_json(bridge.config_file)
        self.assertEqual((config["transport"], config["host"]), ("tcp", "127.0.0.1"))
        replies = self.transact(config, [
            b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n',
            b'{"jsonrpc":"2.0","id":2,"method":"tools/call",'
            b'"params":{"name":"mizu_finish","arguments":{"outcome":"wait","summary":"s"}}}\n',
        ])
        body = json.loads(replies[1]["result"]["content"][0]["text"])
        self.assertEqual(body, {"sealed": True})
        self.assertEqual(seen, [("finish", {"outcome": "wait", "summary": "s"})])

    def test_non_loopback_tcp_endpoint_is_refused(self):
        from mizu.bridge import connect
        with self.assertRaises(Denied):
            connect({"transport": "tcp", "host": "203.0.113.7", "port": 9,
                     "token": "x", "timeout_ms": 1000, "tools": []})

    def test_list_and_call_forward_to_runtime(self):
        seen = []

        def handle(operation, arguments):
            seen.append((operation, arguments))
            return {"sealed": True}

        config = self.bridge_config(handle)
        replies = self.transact(config, [
            b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}\n',
            b'{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n',
            b'{"jsonrpc":"2.0","id":3,"method":"tools/call",'
            b'"params":{"name":"mizu_finish","arguments":{"outcome":"wait","summary":"s"}}}\n',
        ])
        self.assertEqual([t["name"] for t in replies[1]["result"]["tools"]], ["mizu_finish"])
        body = json.loads(replies[2]["result"]["content"][0]["text"])
        self.assertEqual(body, {"sealed": True})
        self.assertFalse(replies[2]["result"]["isError"])
        self.assertEqual(seen, [("finish", {"outcome": "wait", "summary": "s"})])

    def test_unknown_tool_is_error_not_exception(self):
        config = self.bridge_config(lambda op, args: {})
        replies = self.transact(config, [
            b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n',
            b'{"jsonrpc":"2.0","id":2,"method":"tools/call",'
            b'"params":{"name":"mizu_exec","arguments":{}}}\n',
        ])
        self.assertTrue(replies[1]["result"]["isError"])

    def test_refused_operation_is_error_text(self):
        def handle(operation, arguments):
            raise Denied("Role has no capability: finish")

        config = self.bridge_config(handle)
        replies = self.transact(config, [
            b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n',
            b'{"jsonrpc":"2.0","id":7,"method":"tools/call",'
            b'"params":{"name":"mizu_finish","arguments":{"outcome":"wait","summary":"s"}}}\n',
        ])
        self.assertTrue(replies[1]["result"]["isError"])
        self.assertIn("capability", replies[1]["result"]["content"][0]["text"])

    def test_call_before_initialize_is_rejected(self):
        config = self.bridge_config(lambda op, args: {})
        replies = self.transact(config, [
            b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}\n',
        ])
        self.assertIn("error", replies[0])

    def test_oversized_message_is_refused(self):
        config = self.bridge_config(lambda op, args: {})
        source = io.BytesIO(b"x" * (1024 * 1024 + 2) + b"\n")
        with self.assertRaises(Denied):
            serve(input_stream=source, output_stream=io.BytesIO(), bridge=config)

    def test_proxy_runs_as_subprocess(self):
        """The exact `python -m mizu.mcp_proxy` command drivers hand to CLIs."""
        import sys

        def handle(operation, arguments):
            return {"ok": True}

        stdin = (b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
                 b'{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n')
        with Bridge(handle, tool_definitions(("finish",)), timeout=60) as bridge:
            env = environment(extra={"MIZU_BRIDGE_CONFIG": str(bridge.config_file),
                                     "PYTHONPATH": str(ROOT / "src")})
            result = run_process([sys.executable, "-m", "mizu.mcp_proxy"], timeout=30,
                                 maximum=65536, env=env, input_data=stdin)
        self.assertEqual(result.exit_code, 0, result.stderr[-1000:])
        lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual([t["name"] for t in lines[1]["result"]["tools"]], ["mizu_finish"])

    def test_missing_bridge_env_is_refused(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(Denied):
                load_bridge()

    def test_forward_rejects_bad_envelope(self):
        with self.assertRaises(Denied):
            forward({"transport":"unix", "socket": "/nonexistent.sock", "token": "x",
                     "timeout_ms": 1000, "tools": []}, "finish", {"outcome": "wait"})

    def test_json_depth_bound_is_a_product_rule_not_an_interpreter_limit(self):
        from mizu.fs import JSON_DEPTH_REFUSED, MAX_JSON_DEPTH, loads_finite
        within = b"[" * MAX_JSON_DEPTH + b"0" + b"]" * MAX_JSON_DEPTH
        self.assertIsNotNone(loads_finite(within))
        beyond = b"[" * (MAX_JSON_DEPTH + 1) + b"0" + b"]" * (MAX_JSON_DEPTH + 1)
        with self.assertRaises(ValueError) as refused:
            loads_finite(beyond)
        self.assertEqual(str(refused.exception), JSON_DEPTH_REFUSED)
        def nested(depth, kind):
            value = b"0"
            for index in range(depth):
                if kind == "object" or index % 2 == 0:
                    value = b'{"a":' + value + b"}"
                else:
                    value = b"[" + value + b"]"
            return value

        for kind in ("object", "mixed"):
            self.assertIsNotNone(loads_finite(nested(MAX_JSON_DEPTH, kind)), kind)
            with self.assertRaises(ValueError) as refused:
                loads_finite(nested(MAX_JSON_DEPTH + 1, kind))
            self.assertEqual(str(refused.exception), JSON_DEPTH_REFUSED)
        # Brackets inside strings remain text, not nesting.
        self.assertEqual(loads_finite(b'{"a": "' + b"[" * 500 + b'"}'),
                         {"a": "[" * 500})

    def test_deeply_nested_bridge_frames_are_bounded_parse_failures(self):
        from mizu.bridge import connect
        from mizu.fs import JSON_DEPTH_REFUSED, MAX_JSON_DEPTH, loads_finite
        calls = []

        def handle(operation, arguments):
            calls.append((operation, arguments))
            return {}

        bridge = Bridge(handle, tool_definitions(("finish",)), timeout=60, transport="tcp")
        bridge.__enter__()
        self.addCleanup(bridge.__exit__, None, None, None)
        config = read_json(bridge.config_file)
        object_depth = MAX_JSON_DEPTH + 1
        frames = (
            b'{"token": ' + json.dumps(config["token"]).encode()
            + b', "operation": "finish", "arguments": {"nested": '
            + b"[" * 20000 + b"0" + b"]" * 20000 + b"}}\n",
            b'{"token": ' + json.dumps(config["token"]).encode()
            + b', "operation": "finish", "arguments": {"nested": '
            + b'{"a":' * object_depth + b"0" + b"}" * object_depth + b"}}\n",
        )
        for frame in frames:
            with self.subTest(frame_size=len(frame)):
                self.assertLess(len(frame), 1024 * 1024)
                # The product refuses each frame by its own depth bound,
                # before parsing, independent of Python's recursion limit.
                with self.assertRaises(ValueError) as refused:
                    loads_finite(frame)
                self.assertEqual(str(refused.exception), JSON_DEPTH_REFUSED)
                client = connect(config)
                try:
                    client.settimeout(10)
                    client.sendall(frame)
                    data = bytearray()
                    while b"\n" not in data:
                        chunk = client.recv(65536)
                        if not chunk:
                            break
                        data.extend(chunk)
                finally:
                    client.close()
                response = json.loads(data[:data.index(b"\n")].decode("utf-8"))
                self.assertNotIn("Internal tool failure", str(response))
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"], JSON_DEPTH_REFUSED)
        self.assertEqual(calls, [])

    def test_forced_decoder_recursion_error_is_bounded_parse_failure(self):
        from mizu.bridge import connect
        import mizu.bridge as bridge_module
        calls = []

        def handle(operation, arguments):
            calls.append((operation, arguments))
            return {}

        bridge = Bridge(handle, tool_definitions(("finish",)), timeout=60, transport="tcp")
        bridge.__enter__()
        self.addCleanup(bridge.__exit__, None, None, None)
        config = read_json(bridge.config_file)
        frame = (b'{"token": ' + json.dumps(config["token"]).encode()
                 + b', "operation": "finish", "arguments": {}}\n')
        with unittest.mock.patch.object(bridge_module, "loads_finite",
                                       side_effect=RecursionError("forced")):
            client = connect(config)
            try:
                client.settimeout(10)
                client.sendall(frame)
                data = bytearray()
                while b"\n" not in data:
                    chunk = client.recv(65536)
                    if not chunk:
                        break
                    data.extend(chunk)
            finally:
                client.close()
        response = json.loads(data[:data.index(b"\n")].decode("utf-8"))
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "Invalid bridge frame")
        self.assertNotIn("Internal tool failure", response["error"])
        self.assertEqual(calls, [])

    def test_forward_rejects_nonfinite_bridge_response(self):
        import mizu.mcp_proxy as proxy
        from mizu.fs import loads_finite
        payload = b'{"ok": true, "result": {"v": 1e9999}}\n'
        with self.assertRaises(ValueError):
            loads_finite(payload)

        class _FakeBridge:
            def __init__(self, payload):
                self._payload = bytearray(payload)

            def sendall(self, data):
                pass

            def settimeout(self, timeout):
                pass

            def recv(self, size):
                chunk = bytes(self._payload[:size])
                del self._payload[:size]
                return chunk

            def close(self):
                pass

        config = {"transport": "tcp", "host": "127.0.0.1", "port": 1,
                  "token": "x", "timeout_ms": 1000, "tools": []}
        with unittest.mock.patch.object(proxy, "_bridge_connect",
                                        return_value=_FakeBridge(payload)):
            with self.assertRaisesRegex(Denied, "Bridge response was not valid JSON"):
                forward(config, "finish", {"outcome": "wait"})

    def test_trickling_peer_cannot_defer_deadline(self):
        import socket as _socket
        import threading as _threading
        import time as _time
        listener = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        listener.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)
        stop = _threading.Event()
        def serve_once():
            try:
                peer, _ = listener.accept()
            except OSError:
                return
            with peer:
                peer.settimeout(5)
                data = bytearray()
                try:
                    while b"\n" not in data:
                        chunk = peer.recv(65536)
                        if not chunk:
                            return
                        data.extend(chunk)
                    while not stop.is_set():
                        try:
                            peer.sendall(b"x")
                        except OSError:
                            return
                        _time.sleep(0.02)
                except OSError:
                    pass
        worker = _threading.Thread(target=serve_once, daemon=True)
        worker.start()
        self.addCleanup(stop.set)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        config = {"transport": "tcp", "host": "127.0.0.1", "port": port,
                  "token": "x", "timeout_ms": 200, "tools": []}
        start = _time.monotonic()
        with self.assertRaisesRegex(Denied, "deadline"):
            forward(config, "read", {})
        self.assertLess(_time.monotonic() - start, 2.0, "deadline is absolute, not idle")
        stop.set()


class UsageEngineTests(Fixture):
    def write_run(self, name, model):
        run_dir = self.project.root / "runs" / name
        mkdir(run_dir)
        write_json(run_dir / "started.json", {"run": name, "role": "worker"})
        write_json(run_dir / "result.json", {"run": name, "role": "worker", "status": "completed",
                                             "finished_at": "2026-09-01T00:00:00+00:00",
                                             "finish": {}, "model": model, "snapshot": "x"})

    def test_engine_is_recorded_per_entry_and_group(self):
        self.write_run("a" * 32, {"profile": "codex", "provider": "openai", "model": "m",
                                  "engine": "codex", "requests": 1,
                                  "usage": [{"input_tokens": 3, "output_tokens": 1}]})
        facts = summarize(self.project)
        self.assertEqual(facts["recent_entries"][0]["engine"], "codex")
        self.assertEqual(facts["groups"][0]["engines"], ["codex"])

    def test_incomplete_records_without_engine_stay_honest(self):
        self.write_run("b" * 32, {"profile": "primary", "provider": "acme", "model": "m1",
                                  "requests": 1, "usage": []})
        facts = summarize(self.project)
        self.assertEqual(facts["recent_entries"][0]["engine"], "unknown")

class CurrentFormatTests(Fixture):
    def test_mcp_count_is_bounded_before_launch(self):
        extra='\n'.join(f'[profiles.alternate.mcp_servers.server{i}]\nurl="https://example.invalid/mcp"' for i in range(65))
        with self.assertRaises(ConfigError):extend_config(self,'\n'+extra)

    def test_native_plugin_content_separates_session_identity(self):
        directory=self.root/'plugin';directory.mkdir()
        content=directory/'plugin.json';content.write_text('{"name":"first"}')
        ctx=self.context('consult');profile=ctx.role.profile
        raw={**self.config.profiles[profile],'engine':'claude','session':'persistent',
             'options':{'plugins':[{'type':'local','path':str(directory)}]}}
        config=dataclasses.replace(self.config,profiles={**self.config.profiles,profile:raw})
        ctx.config=config
        first,_=session_record(ctx,profile,effective(config,ctx.role,profile))
        content.write_text('{"name":"changed"}')
        second,_=session_record(ctx,profile,effective(config,ctx.role,profile))
        self.assertNotEqual(first,second)

    def test_internal_publication_has_no_generation_numbers(self):
        import tomllib
        from mizu.runtime import Engine
        from support import ScriptDriver
        self.assertNotIn('schema',tomllib.loads(self.file.read_text()))
        self.assertEqual(set(tomllib.loads((self.project.root/'project.toml').read_text())),{'roles','verify'})
        snapshot=self.project.snapshots.get()
        self.assertNotIn('schema',snapshot)
        result=Engine(self.config,driver=ScriptDriver()).run(self.project,'consult')
        self.assertNotIn('schema',result)
        with Bridge(lambda op,args:{},[],timeout=2) as bridge:
            self.assertNotIn('protocol',read_json(bridge.config_file))

    def test_tree_resource_digest_and_symlink_refusal(self):
        from mizu.engine_config import resource_digest
        tree=self.root/'reviewed-plugin';tree.mkdir()
        file=tree/'plugin.json';file.write_text('{"name":"test"}')
        original=resource_digest(tree)
        file.write_text('{"name":"changed"}')
        self.assertNotEqual(original,resource_digest(tree))
        (tree/'link').symlink_to(file)
        with self.assertRaises(ConfigError):resource_digest(tree)

    def test_stdio_mcp_uses_existing_oci_floor(self):
        from mizu.engine_config import connected_servers
        ctx=self.context('consult')
        settings={'mcp_servers':{'test':{'command':'/usr/bin/test-mcp','args':['literal;not shell']}}}
        server=connected_servers(ctx,settings)['test']
        argv=[server['command'],*server['args']]
        self.assertIn('--read-only',argv)
        self.assertIn('--cap-drop=ALL',argv)
        self.assertIn('-i',argv)
        self.assertIn('exec /usr/bin/test-mcp',argv[-1])
        self.assertNotEqual(server['command'],'/usr/bin/test-mcp')
        from mizu.engine_config import stop_engine_containers
        from mizu.process import Result
        names=list(ctx.engine_containers)
        with unittest.mock.patch('mizu.process.run',return_value=Result(0,'','','exit',.1)) as remove:
            stop_engine_containers(ctx)
        self.assertEqual(ctx.engine_containers,[])
        self.assertIn(names[0],remove.call_args.args[0])
        ctx.engine_containers=names
        with unittest.mock.patch('mizu.process.run',return_value=Result(1,'','mock cleanup failure','exit',.1)):
            with self.assertRaises(Denied):stop_engine_containers(ctx)
