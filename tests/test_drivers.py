"""Contract tests for the generic inference-engine registry (ADR-007).

Covers routing, trusted argv, per-run confinement, usage honesty and the
shared MCP proxy. Fake CLIs stand in for real binaries; nothing here proves
live-provider behavior (see docs/testing.md for the separate live gates).
"""
import dataclasses
import io
import json
import os
import unittest
import unittest.mock
from pathlib import Path
from support import Fixture, ROOT
from mizu.bridge import Bridge
from mizu.claude import ClaudeDriver, allowed_tools, build_argv as claude_argv, parse_events as claude_parse
from mizu.codex import CodexDriver, build_argv as codex_argv, build_config_toml, parse_events as codex_parse
from mizu.config import Config, load
from mizu.doctor import check
from mizu.drivers import admit_invocation, command_for, driver_for
from mizu.errors import ConfigError, Denied, LimitExceeded, ProtocolError
from mizu.fs import mkdir, read_json, write_json
from mizu.mcp_proxy import forward, load_bridge, serve
from mizu.pi import PiDriver
from mizu.process import Result, environment
from mizu.process import run as run_process
from mizu.protocol import tool_definitions
from mizu.runtime import Engine
from mizu.usage import summarize


def extend_config(fixture, extra, name="alt.toml"):
    path = fixture.root / "config" / name
    path.write_text(fixture.file.read_text() + extra)
    return load(path)


class EngineConfigTests(Fixture):
    def test_default_engine_is_pi(self):
        self.assertEqual(self.config.engine("primary"), "pi")
        self.assertEqual(self.config.engine("alternate"), "pi")

    def test_unknown_engine_rejected(self):
        with self.assertRaises(ConfigError):
            extend_config(self, '\n[profiles.bad]\nprovider = "p"\nmodel = "m"\nengine = "flux"\n')

    def test_unknown_profile_rejected(self):
        with self.assertRaises(ConfigError):
            self.config.engine("missing")

    def test_empty_engine_command_rejected(self):
        text = self.file.read_text().replace('codex_command = ["codex"]', 'codex_command = []')
        path = self.root / "config" / "empty.toml"
        path.write_text(text)
        with self.assertRaises(ConfigError):
            load(path)

    def test_command_for_unknown_engine_rejected(self):
        with self.assertRaises(ConfigError):
            command_for(self.config, "flux")


class RegistryTests(Fixture):
    def setUp(self):
        super().setUp()
        self.multi = extend_config(
            self, '\n[profiles.codex]\nprovider = "openai"\nmodel = "m"\nengine = "codex"\n'
                  '\n[profiles.claude]\nprovider = "anthropic"\nmodel = "m"\nengine = "claude"\n')

    def test_driver_for_routes_by_engine(self):
        cache = {}
        self.assertIsInstance(driver_for(self.multi, "primary", cache), PiDriver)
        self.assertIsInstance(driver_for(self.multi, "codex", cache), CodexDriver)
        self.assertIsInstance(driver_for(self.multi, "claude", cache), ClaudeDriver)
        self.assertIs(driver_for(self.multi, "codex", cache), cache["codex"])

    def test_engine_resolve_prefers_injected_driver(self):
        from support import ScriptDriver
        engine = Engine(self.multi, driver=ScriptDriver())
        self.assertIs(engine.resolve("codex"), engine.driver)
        fresh = Engine(self.multi)
        self.assertIsInstance(fresh.resolve("primary"), PiDriver)

    def test_admit_invocation_is_idempotent(self):
        context = self.context("consult")
        admit_invocation(context)
        admit_invocation(context)
        self.assertEqual(context.request_count, 1)
        self.assertEqual(read_json(context.run_dir / "admission.json")["requests"], 1)

    def test_admit_invocation_enforces_shared_budget(self):
        context = self.context("consult")
        context.config = dataclasses.replace(
            self.config, limits=dataclasses.replace(self.config.limits, daily_requests=0))
        with self.assertRaises(LimitExceeded):
            admit_invocation(context)
        self.assertEqual(context.request_count, 0)

    def test_admit_invocation_enforces_per_run_bound(self):
        context = self.context("consult")
        context.config = dataclasses.replace(
            self.config, limits=dataclasses.replace(self.config.limits, requests_per_run=0))
        with self.assertRaises(LimitExceeded):
            admit_invocation(context)


class CodexDriverTests(Fixture):
    def test_argv_is_hardened(self):
        argv = codex_argv(("codex",))
        for token in ("exec", "--json", "--sandbox", "read-only", "--ask-for-approval",
                      "never", "--skip-git-repo-check", "--ephemeral", "-"):
            self.assertIn(token, argv)
        text = " ".join(argv)
        self.assertNotIn("--yolo", text)
        self.assertNotIn("danger-full-access", text)
        self.assertNotIn("dangerously-bypass", text)

    def test_argv_refuses_unsafe_command(self):
        with self.assertRaises(ConfigError):
            codex_argv(("codex", "--yolo"))

    def test_config_confines_tools_and_disables_host_powers(self):
        import tomllib
        text = build_config_toml(model="m", instructions=Path("/tmp/i.md"),
                                 capabilities=("files", "read", "finish"),
                                 bridge_file=Path("/tmp/b.json"))
        parsed = tomllib.loads(text)
        self.assertEqual(parsed["model"], "m")
        self.assertEqual(parsed["approval_policy"], "never")
        self.assertEqual(parsed["sandbox_mode"], "read-only")
        self.assertTrue(parsed["mcp_servers"]["mizu"]["required"])
        self.assertEqual(parsed["mcp_servers"]["mizu"]["enabled_tools"],
                         ["mizu_files", "mizu_read", "mizu_finish"])
        self.assertIn('"mizu_files"', text)
        self.assertIn('"mizu_finish"', text)
        self.assertNotIn('"mizu_exec"', text)
        self.assertIn('shell_tool = false', text)
        self.assertIn('multi_agent = false', text)
        self.assertIn('web_search = "disabled"', text)
        self.assertIn('persistence = "none"', text)
        self.assertIn('approval_policy = "never"', text)
        self.assertIn('sandbox_mode = "read-only"', text)

    def test_parse_events_collects_usage(self):
        stdout = ('{"type":"thread.started","thread_id":"t1"}\n'
                  'not json\n'
                  '{"type":"item.completed","item":{}}\n'
                  '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":3}}\n')
        parsed = codex_parse(stdout)
        self.assertEqual(parsed["conversation_id"], "t1")
        self.assertEqual(parsed["usage"], [{"input_tokens": 10, "output_tokens": 3}])
        self.assertEqual(parsed["bad_lines"], 1)
        self.assertEqual(parsed["errors"], [])

    def test_parse_events_surfaces_errors(self):
        parsed = codex_parse('{"type":"error","message":"rate limited"}\n')
        self.assertEqual(parsed["errors"], ["rate limited"])

    def run_driver(self, stdout):
        context = self.context("consult")

        def fake_runner(argv, *, timeout, maximum, cwd, env, cancel, input_data=b""):
            self.assertIn("--ephemeral", argv)
            self.assertEqual(env.get("CODEX_HOME"), str(context.run_dir / "codex-home"))
            context.handle("finish", {"outcome": "wait", "summary": "Observed via bridge"})
            return Result(0, stdout, "", "exited", 0.1)

        driver = CodexDriver(self.config, runner=fake_runner)
        return driver, driver.execute(context, '{"goal": "probe"}')

    def test_execute_round_trips_finish_and_records_usage(self):
        stdout = ('{"type":"thread.started","thread_id":"t9"}\n'
                  '{"type":"turn.completed","usage":{"input_tokens":7,"output_tokens":2}}\n')
        _, result = self.run_driver(stdout)
        self.assertEqual(result["engine"], "codex")
        self.assertEqual(result["requests"], 1)
        self.assertEqual(result["conversation_id"], "t9")
        self.assertEqual(result["usage"], [{"input_tokens": 7, "output_tokens": 2}])

    def test_execute_writes_evidence_files(self):
        stdout = '{"type":"thread.started","thread_id":"t1"}\n'
        _, result = self.run_driver(stdout)
        matches = [p for p in (self.project.root / "runs").glob("*")
                   if (p / "codex-events.jsonl").exists() and (p / "diagnostics.txt").exists()
                   and (p / "codex-home" / "config.toml").exists()]
        self.assertEqual(len(matches), 1)
        self.assertIn("thread.started", (matches[0] / "codex-events.jsonl").read_text())

    def test_execute_without_finish_is_refused(self):
        context = self.context("consult")

        def silent(argv, *, timeout, maximum, cwd, env, cancel, input_data=b""):
            return Result(0, '{"type":"thread.started","thread_id":"t1"}\n', "", "exited", 0.1)

        with self.assertRaises(ProtocolError):
            CodexDriver(self.config, runner=silent).execute(context, "x")

    def test_execute_failure_is_loud(self):
        context = self.context("consult")

        def broken(argv, *, timeout, maximum, cwd, env, cancel, input_data=b""):
            return Result(1, "", "auth required", "exited", 0.1)

        with self.assertRaises(ProtocolError):
            CodexDriver(self.config, runner=broken).execute(context, "x")

    def test_execute_cancelled_is_cancelled(self):
        context = self.context("consult")
        context.stop.set()

        def slow(argv, *, timeout, maximum, cwd, env, cancel, input_data=b""):
            return Result(0, "", "", "exited", 0.1)

        from mizu.errors import Cancelled
        with self.assertRaises(Cancelled):
            CodexDriver(self.config, runner=slow).execute(context, "x")


class ClaudeDriverTests(Fixture):
    def test_allowlist_is_explicit_mcp_only(self):
        tools = allowed_tools(("files", "read", "finish"))
        self.assertEqual(tools, ["mcp__mizu__mizu_files", "mcp__mizu__mizu_read", "mcp__mizu__mizu_finish"])
        for tool in tools:
            self.assertTrue(tool.startswith("mcp__mizu__"))
        self.assertNotIn("Bash", " ".join(tools))

    def test_argv_pins_policy_and_forbids_skip(self):
        from mizu.claude import build_mcp_config
        tools = allowed_tools(("read", "finish"))
        argv = claude_argv(("claude",), mcp_config=Path("/tmp/m.json"), tools=tools,
                           system_prompt="policy", prompt="work")
        self.assertIn("-p", argv)
        self.assertIn("stream-json", argv)
        self.assertIn("--mcp-config", argv)
        self.assertIn("--allowedTools", argv)
        self.assertIn("--system-prompt", argv)
        text = " ".join(argv)
        self.assertNotIn("--dangerously-skip-permissions", text)
        self.assertNotIn("--yolo", text)
        config = json.loads(build_mcp_config(Path("/tmp/b.json")))
        self.assertIn("mizu", config["mcpServers"])
        self.assertEqual(config["mcpServers"]["mizu"]["args"], ["-m", "mizu.mcp_proxy"])

    def test_parse_events_collects_usage_and_session(self):
        stdout = ('{"type":"system","session_id":"s1"}\n'
                  '{"type":"result","usage":{"input_tokens":5,"output_tokens":2}}\n')
        parsed = claude_parse(stdout)
        self.assertEqual(parsed["session_id"], "s1")
        self.assertEqual(parsed["usage"], [{"input_tokens": 5, "output_tokens": 2}])

    def test_execute_round_trips_finish(self):
        context = self.context("consult")
        stdout = '{"type":"result","session_id":"s2","usage":{"input_tokens":1,"output_tokens":1}}\n'

        def fake_runner(argv, *, timeout, maximum, cwd, env, cancel):
            self.assertIn("-p", argv)
            context.handle("finish", {"outcome": "wait", "summary": "Observed via bridge"})
            return Result(0, stdout, "", "exited", 0.1)

        result = ClaudeDriver(self.config, runner=fake_runner).execute(context, "work")
        self.assertEqual(result["engine"], "claude")
        self.assertEqual(result["session_id"], "s2")
        self.assertEqual(result["requests"], 1)

    def test_execute_without_finish_is_refused(self):
        context = self.context("consult")

        def silent(argv, *, timeout, maximum, cwd, env, cancel):
            return Result(0, "{}\n", "", "exited", 0.1)

        with self.assertRaises(ProtocolError):
            ClaudeDriver(self.config, runner=silent).execute(context, "work")


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
            forward({"protocol": 1, "socket": "/nonexistent.sock", "token": "x",
                     "timeout_ms": 1000, "tools": []}, "finish", {"outcome": "wait"})


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

    def test_legacy_records_without_engine_stay_honest(self):
        self.write_run("b" * 32, {"profile": "primary", "provider": "acme", "model": "m1",
                                  "requests": 1, "usage": []})
        facts = summarize(self.project)
        self.assertEqual(facts["recent_entries"][0]["engine"], "unknown")


class DoctorEngineTests(Fixture):
    def statuses(self, config):
        return {c["name"]: c["status"] for c in check(config)["checks"]}

    def test_unused_engines_are_not_run(self):
        statuses = self.statuses(self.config)
        self.assertEqual(statuses["codex CLI"], "not_run")
        self.assertEqual(statuses["claude CLI"], "not_run")

    def test_proposal_stores_pass_when_clean(self):
        self.assertEqual(self.statuses(self.config)["proposal stores"], "pass")

    def test_proposal_stores_flag_unreadable(self):
        inbox = self.project.root / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "bad.json").write_text("{corrupt")
        checks = {c["name"]: c for c in check(self.config)["checks"]}
        entry = checks["proposal stores"]
        self.assertEqual(entry["status"], "fail")
        self.assertIn("sample", entry["details"])

    def test_missing_configured_engine_fails_loudly(self):
        text = self.file.read_text().replace('codex_command = ["codex"]',
                                             'codex_command = ["mizu-missing-binary-xyz"]')
        text += '\n[profiles.codex]\nprovider = "openai"\nmodel = "m"\nengine = "codex"\n'
        path = self.root / "config" / "codex.toml"
        path.write_text(text)
        statuses = self.statuses(load(path))
        self.assertEqual(statuses["codex CLI"], "fail")
        self.assertEqual(statuses["claude CLI"], "not_run")

    def test_driver_flag_contracts_match_compat_files(self):
        from mizu.claude import allowed_tools, build_argv as claude_argv
        from mizu.codex import build_argv as codex_argv
        codex = " ".join(codex_argv(("codex",)))
        claude = " ".join(claude_argv(("claude",), mcp_config=Path("/tmp/m.json"),
                                      tools=allowed_tools(("read", "finish")),
                                      system_prompt="policy", prompt="work"))
        for engine, argv_text in (("codex", codex), ("claude", claude)):
            compat = read_json(ROOT / "adapters" / engine / "compatibility.json")
            required = compat["required_flags"]
            for flag in required:
                self.assertIn(flag, argv_text, f"{engine} argv dropped {flag}")
            forbiddens = compat.get("forbidden_argv", [])
            for flag in forbiddens:
                self.assertNotIn(flag, argv_text, f"{engine} argv allows {flag}")
            self.assertEqual(sorted(required), sorted(set(required)))


if __name__ == "__main__":
    raise SystemExit(unittest.main())
