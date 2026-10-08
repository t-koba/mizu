"""Current engine contracts using synthetic peers, never live inference."""
import dataclasses
import io
import json
import os
import sys
import unittest
import unittest.mock
from pathlib import Path
from support import Fixture, ROOT
from mizu.bridge import Bridge
from mizu.claude import ClaudeDriver, model_delta, terminal_result
from mizu.codex import CodexDriver, TokenUsage
from mizu.config import load
from mizu.drivers import admit_invocation, command_for, driver_for
from mizu.engine_config import effective, session_record, save_session
from mizu.errors import ConfigError, Denied, LimitExceeded, ProtocolError, ModelFailure
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
        self.assertTrue(ctx.handle('_engine_tool',{'name':'tool'})['allowed'])
        with self.assertRaises(Denied): ctx.handle('_engine_tool',{'name':'other'})
        ctx.handle('finish',{'outcome':'wait','summary':'done'})
        with self.assertRaises(Denied): ctx.handle('_engine_tool',{'name':'tool'})
        with self.assertRaises(Denied): ctx.handle('_budget',{'sequence':1})


class SyntheticEngineTests(Fixture):
    def execute(self,engine,scenario='normal',persistent=False):
        ctx=self.context('consult')
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

    def test_both_drivers_require_seal_and_record_honest_units(self):
        for engine,unit in (('codex','turn'),('claude','query')):
            ctx,result=self.execute(engine)
            self.assertEqual(result['request_unit'],unit)
            self.assertEqual(result['requests'],1)
            self.assertTrue(result['usage_known'])
            self.assertIsNotNone(ctx.finished)
            self.assertTrue((ctx.run_dir/(engine+'-events.jsonl')).is_file())
            with self.assertRaises(ProtocolError): self.execute(engine,'missing-finish')

    def test_codex_terminal_failure_and_unanswerable_request(self):
        for scenario in ('failed','interrupted','approval','mcp-failure','bad-json','exit'):
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
        for profile, engine, scenario in (('primary', 'codex', 'provider-error'), ('alternate', 'claude', 'normal')):
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
        self.assertEqual(first['status'], 'deferred')
        self.assertEqual(second['status'], 'completed')
        self.assertEqual(second['model']['engine'], 'claude')
        self.assertEqual(second['selection']['profile'], 'alternate')
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

    def test_post_finish_calls_are_refused(self):
        for engine in ('codex','claude'):
            self.execute(engine,'post-finish')
        with self.assertRaises(Denied): self.execute('codex','native-after-finish')

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
