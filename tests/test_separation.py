"""Separation regression tests: mechanism enforces bounds, policy selects values.

Covers the reviewer sweep without weakening safety invariants
(done still needs verification, single-writer/locks/root refusals unchanged).
Also pins safe deduplications: one MCP loop, one terminate path, one profile
lookup, runtime-owned configuration, table-driven limits, shared service scheduling.
"""
import dataclasses
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support import Fixture, ROOT
from mizu.errors import ConfigError, Denied
from mizu.fs import read_json, write_json, mkdir


class DedupContractTests(unittest.TestCase):
    def test_single_mcp_loop(self):
        import mizu.editor
        import mizu.mcp_loop
        import mizu.mcp_proxy
        import mizu.protocol
        from mizu import fs
        self.assertIs(mizu.mcp_loop.MCP_VERSIONS, mizu.protocol.MCP_VERSIONS)
        self.assertIs(mizu.mcp_loop.MAX_FRAME, fs.MAX_FRAME)
        self.assertTrue(callable(mizu.mcp_loop.serve_stdio))
    def test_single_terminate_path(self):
        import mizu.process
        with patch("mizu.platform.terminate_process") as terminate:
            process = object()
            mizu.process.terminate(process, grace=.5)
            terminate.assert_called_once_with(process, grace=.5)

    def test_single_profile_lookup(self):
        from mizu.config import load
        with tempfile.TemporaryDirectory() as td:
            from mizu.cli import configure
            path = Path(td) / "config.toml"
            configure(path, None)
            config = load(path)
            for method in (config.model, config.engine):
                with self.assertRaises(ConfigError):
                    method("absent")

    def test_native_options_cannot_replace_bridge(self):
        from mizu.engine_config import effective
        import dataclasses
        fixture=Fixture();fixture.setUp();self.addCleanup(fixture.doCleanups)
        ctx=fixture.context();profile=ctx.role.profile
        raw={**fixture.config.profiles[profile],'options':{'systemPrompt':'override'}}
        config=dataclasses.replace(fixture.config,profiles={**fixture.config.profiles,profile:raw})
        with self.assertRaises(ConfigError):effective(config,ctx.role,profile)

    def test_dispatch_and_cli_are_tabled(self):
        from mizu.runtime import Context
        for op in ("diff", "files", "read", "exec", "experiment", "verify",
                   "fetch", "search", "insights", "decide", "submit_insight",
                   "consult", "report", "finish"):
            self.assertIn(op, Context._DISPATCH)
        from mizu.cli import _PROJECT_COMMANDS
        for cmd in ("status", "arm", "run", "insight", "backup", "prune"):
            self.assertIn(cmd, _PROJECT_COMMANDS)

    def test_services_share_scheduling(self):
        import mizu.services
        self.assertTrue(callable(mizu.services._scheduled_roles))



class ProtocolWordingTests(unittest.TestCase):
    def test_definitions_carry_no_normative_policy_language(self):
        from mizu.protocol import DEFINITIONS
        banned = ["honestly", "should", "must", "preserve the question",
                  "do not override", "not instructions"]
        for name, (description, _) in DEFINITIONS.items():
            lowered = description.lower()
            for word in banned:
                self.assertNotIn(word, lowered, f"{name} carries policy wording: {description}")

    def test_experiment_records_without_question_fields(self):
        from mizu.protocol import DEFINITIONS, validate
        validate({"script": "echo hi"}, DEFINITIONS["experiment"][1])
        validate({"script": "x", "question": "q", "comparison": "c", "measure": "m"},
                 DEFINITIONS["experiment"][1])

    def test_decide_still_requires_reason_and_defer_revisit(self):
        # Protocol schema keeps length bounds; the non-empty/revisit rules live
        # in the decision mechanism (insights.decide), not in wording.
        from mizu.protocol import DEFINITIONS, validate
        validate({"id": "x", "action": "accept", "reason": "ok", "rev": 2}, DEFINITIONS["decide"][1])
        import tempfile
        from pathlib import Path as _P
        from mizu.insights import Insights
        tmp = _P(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        for sub in ("inbox", "decisions", "decision-history", "locks"):
            (tmp / sub).mkdir(parents=True, exist_ok=True)
        store = Insights(tmp)
        rec = store.submit(source="s", title="T", body="b", base_snapshot=None)
        with self.assertRaises(Denied):
            store.decide(rec["id"], "accept", "   ", "", "test", expected_rev=1)
        with self.assertRaises(Denied):
            store.decide(rec["id"], "defer", "later", "   ", "test", expected_rev=1)


class ConsultGateTests(Fixture):
    def test_single_shared_gate(self):
        import mizu.runtime as runtime
        import mizu.smoke as smoke
        # The root refusal is orthogonal to the shared-gate assertion; run the
        # gate logic as an unprivileged user even when the suite runs as root.
        with patch('mizu.smoke._platform.is_root', return_value=False), \
                patch.object(runtime,'check_consult_role',side_effect=Denied('shared gate')) as gate:
            with self.assertRaisesRegex(Denied,'shared gate'):
                smoke.live(self.config,role_name='consult')
            gate.assert_called_once_with('consult',self.config.roles['consult'])

    def test_readonly_insights_and_diff_allowed(self):
        import dataclasses
        from mizu.runtime import Engine, check_consult_role
        from support import ScriptDriver
        base = self.config.roles["consult"]
        for caps in (("files", "read", "finish", "insights"),
                     ("files", "read", "finish", "diff"),
                     ("files", "read", "finish", "insights", "diff", "fetch", "search")):
            role = dataclasses.replace(base, name="advisor", capabilities=caps)
            check_consult_role("advisor", role)  # must not raise
            config = dataclasses.replace(self.config, roles={**self.config.roles, "advisor": role})
            result = Engine(config, driver=ScriptDriver()).consult(
                self.context(), {"question": "q?", "role": "advisor"})
            self.assertEqual(result["role"], "advisor")

    def test_write_caps_still_refused_before_model(self):
        from mizu.runtime import Engine, check_consult_role
        import dataclasses
        from support import ScriptDriver
        base = self.config.roles["consult"]
        for caps in (("files", "read", "finish", "exec"),
                     ("files", "read", "finish", "decide"),
                     ("files", "read", "finish", "report"),
                     ("files", "read", "finish", "consult")):
            role = dataclasses.replace(base, name="bad", capabilities=caps)
            with self.assertRaises(ConfigError):
                check_consult_role("bad", role)
        worker = self.config.roles["worker"]
        with self.assertRaises(ConfigError):
            Engine(self.config, driver=ScriptDriver()).consult(
                self.context(), {"question": "q?", "role": "worker"})

    def test_consult_default_is_configuration_fail_closed(self):
        import dataclasses
        from mizu.errors import ConfigError, Denied
        from mizu.runtime import Engine
        from support import ScriptDriver
        # Configured default answers when the caller names no role.
        result = Engine(self.config, driver=ScriptDriver()).consult(
            self.context(), {"question": "q?"})
        self.assertEqual(result["role"], self.config.consult_role)
        # Unset default fails closed instead of naming a role.
        bare = dataclasses.replace(self.config, consult_role="")
        with self.assertRaises(Denied):
            Engine(bare, driver=ScriptDriver()).consult(
                self.context(), {"question": "q?"})
        # Unknown configured default fails at load, not at call time.
        text = self.file.read_text()
        path = self.root / "config/unknown-consult-role.toml"
        path.write_text(text.replace('consult_role = "consult"', 'consult_role = "ghost"'))
        from mizu.config import load
        with self.assertRaises(ConfigError):
            load(path)

    def test_smoke_default_is_configuration_fail_closed(self):
        import dataclasses
        import mizu.smoke as smoke
        from mizu.errors import ConfigError
        from unittest.mock import patch
        # Explicit roles still work; unset default on multi-role configs
        # requires --role instead of naming one.
        bare = dataclasses.replace(self.config, consult_role="")
        with patch("mizu.smoke._platform.is_root", return_value=False):
            with self.assertRaises(ConfigError):
                smoke.live(bare)

    def test_writable_role_requires_verify_capability(self):
        from mizu.config import load
        text = self.file.read_text()
        # worker is writable and already has verify; strip it -> load must fail.
        broken = text.replace('"verify", ', '').replace(', "verify"', '')
        path = self.root / "config/broken.toml"
        path.write_text(broken)
        with self.assertRaises(ConfigError):
            load(path)


class InsightRetentionTests(Fixture):
    def test_list_keeps_newest_behind_bound(self):
        import json
        ids = []
        for n in range(35):
            item = self.project.insights.submit(
                source="searcher", title=f"T{n:02d}", body="b", base_snapshot=None)
            ids.append(item["id"])
        # Give distinct, sortable timestamps so "newest" is well-defined
        # (now() has second precision; rapid submits would tie).
        for n, insight_id in enumerate(ids):
            path = self.project.root / "inbox" / f"{insight_id}.json"
            record = json.loads(path.read_text())
            record["created_at"] = f"2026-01-01T00:00:{n:02d}+00:00"
            path.write_text(json.dumps(record))
        listed = self.project.insights.list(limit=30)
        self.assertEqual(len(listed), 30)
        titles = [r["title"] for r in listed]
        self.assertIn("T34", titles)
        self.assertNotIn("T00", titles)

    def test_pending_insights_knob_controls_prompt(self):
        import dataclasses
        import json
        from mizu.runtime import prompt_for
        for n in range(5):
            self.project.insights.submit(source="searcher", title=f"K{n}", body="b",
                                         base_snapshot=None)
        small = dataclasses.replace(self.config, limits=dataclasses.replace(
            self.config.limits, pending_insights=2))
        ctx = self.context("worker")
        ctx.config = small
        prompt = json.loads(prompt_for(ctx))
        self.assertEqual(len(prompt["pending_insights"]), 2)

    def test_retention_days_knob_and_zero_disables(self):
        import os
        import time
        old = self.project.insights.submit(source="s", title="Old", body="b", base_snapshot=None)
        self.project.insights.decide(old["id"], "reject", "no", "", "test", expected_rev=1)
        ancient = time.time() - 32 * 86400
        from mizu.fs import read_json, write_json, mkdir
        import datetime
        decision_path = self.project.root / "decisions" / f"{old['id']}.json"
        decision = read_json(decision_path)
        decision["created_at"] = datetime.datetime.fromtimestamp(ancient, datetime.timezone.utc).isoformat()
        write_json(decision_path, decision)
        self.assertEqual(self.project.insights.gc_decided(keep_days=60), 0)
        self.assertEqual(self.project.insights.gc_decided(keep_days=31), 1)
        # Zero disables: file must survive even when old.
        again = self.project.insights.submit(source="s", title="Old2", body="b", base_snapshot=None)
        self.project.insights.decide(again["id"], "reject", "no", "", "test", expected_rev=1)
        os.utime(self.project.root / "inbox" / f"{again['id']}.json", (ancient, ancient))
        self.assertEqual(self.project.insights.gc_decided(keep_days=0), 0)
        self.assertTrue((self.project.root / "inbox" / f"{again['id']}.json").exists())

    def test_budget_retention_knob(self):
        import datetime as dt
        import tempfile
        from mizu.budget import Budget
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        today = dt.datetime.now(dt.timezone.utc).date()
        forty = (today - dt.timedelta(days=40)).isoformat()
        (tmp / f"{forty}.json").write_text('{"day": "%s", "requests": ["old"]}' % forty)
        # 40 days old survives a 60-day window but not the default 31-day one.
        self.assertEqual(Budget(tmp, 100, retention_days=60).gc(), 0)
        self.assertTrue((tmp / f"{forty}.json").exists())
        self.assertEqual(Budget(tmp, 100, retention_days=31).gc(), 1)

    def test_config_accepts_new_knobs(self):
        from mizu.config import load
        text = self.file.read_text() + "\n"
        path = self.root / "config/knobs.toml"
        path.write_text(self.file.read_text().replace("pending_insights = 30", "pending_insights = 5")
                        .replace("retention_days = 31", "retention_days = 60"))
        config = load(path)
        self.assertEqual(config.limits.pending_insights, 5)
        self.assertEqual(config.limits.retention_days, 60)


class SandboxPolicyTests(unittest.TestCase):
    def setUp(self):
        import json
        from mizu.cli import configure
        from mizu.config import load
        self.temp = tempfile.TemporaryDirectory(prefix="mizu-sep-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        config_file = self.root / "config.toml"
        configure(config_file, None)
        text = config_file.read_text().replace('data_dir = "~/.local/state/mizu"',
                                                'data_dir = ' + json.dumps(str(self.root / "data")))
        text = text.replace('provider = ""', 'provider = "p"').replace('model = ""', 'model = "m"')
        text = text.replace('\ndaily_requests = 0', '\ndaily_requests = 1').replace('free_disk_mb = 1024', 'free_disk_mb = 0')
        text = text.replace('image = ""', 'image = "sha256:' + 'a' * 64 + '"')
        config_file.write_text(text)
        self.file = config_file
        from mizu.config import load as _load
        self.config = _load(config_file)

    def test_env_token_boundary(self):
        from mizu.cli import configure
        from mizu.config import load
        from mizu.errors import ConfigError
        # Allowed: MONKEY_PATH, TOKENIZERS_*, KEYCLOAK_*
        for key in ("MONKEY_PATH", "TOKENIZERS_PARALLELISM", "KEYCLOAK_URL", "HF_HOME"):
            path = self.root / f"env-{key}.toml"
            path.write_text(self.file.read_text() + f'\n[sandbox.env]\n{key} = "x"\n')
            load(path)  # must not raise
        # Refused: exact tokens, runtime vars, and process-startup hijack vars.
        for key in ("HF_TOKEN", "MY_SECRET", "API_KEY", "PUBLIC_KEY_PATH", "HOME", "PATH",
                    "NODE_OPTIONS", "LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH", "BASH_ENV"):
            path = self.root / f"env-bad-{key}.toml"
            path.write_text(self.file.read_text() + f'\n[sandbox.env]\n{key} = "x"\n')
            with self.assertRaises(ConfigError, msg=key):
                load(path)

    def test_mount_target_root_refused(self):
        from mizu.config import load
        from mizu.errors import ConfigError
        for target in ("/root/x", "/root"):
            path = self.root / "mount.toml"
            path.write_text(self.file.read_text() +
                            f'\n[[sandbox.mounts]]\nsource = "/tmp"\ntarget = "{target}"\n')
            with self.assertRaises(ConfigError, msg=target):
                load(path)


class WebSeparationTests(Fixture):
    def test_fragment_stripped_not_denied(self):
        from mizu.web import validate_url
        self.assertEqual(validate_url("https://example.org/a/doc#section-3", ["example.org"]),
                         ("example.org", "/a/doc"))

    def test_all_feeds_consulted_with_truncated_flag(self):
        from mizu.web import Web
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        feeds = [f"https://example.org/f{n}" for n in range(40)]
        web = Web({"hosts": ["example.org"], "feeds": feeds, "cache_seconds": 0,
                   "timeout_seconds": 1, "max_bytes": 1024, "search_command": [],
                   "intranet": False}, tmp / "cache", tmp / "rcpt")
        seen = []
        def fake_fetch(url):
            seen.append(url)
            return {"id": "r", "text": "<rss><channel><item><title>hi</title>"
                    "<link>https://example.org/x</link><description>d</description></item></channel></rss>"}
        web.fetch = fake_fetch
        result = web.search("hi")
        self.assertEqual(len(seen), 40)
        self.assertEqual(result["feeds_total"], 40)
        self.assertIn("truncated", result)

    def test_feed_overflow_truncates_with_all_feeds_consulted(self):
        from mizu.web import Web, SEARCH_RESULT_LIMIT
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        feeds = [f"https://example.org/f{n}" for n in range(40)]
        web = Web({"hosts": ["example.org"], "feeds": feeds, "cache_seconds": 0,
                   "timeout_seconds": 1, "max_bytes": 1024, "search_command": [],
                   "intranet": False}, tmp / "cache", tmp / "rcpt")
        seen = []
        items = "".join("<item><title>hi</title><link>https://example.org/x</link>"
                        "<description>d</description></item>" for _ in range(30))
        def fake_fetch(url):
            seen.append(url)
            return {"id": "r", "text": f"<rss><channel>{items}</channel></rss>"}
        web.fetch = fake_fetch
        result = web.search("hi")
        self.assertEqual(len(seen), 40)
        self.assertEqual(result["feeds_consulted"], 40)
        self.assertEqual(len(result["results"]), SEARCH_RESULT_LIMIT)
        self.assertTrue(result["truncated"])

    def test_adapter_results_page_up_to_documented_limit(self):
        from mizu.web import Web, SEARCH_RESULT_LIMIT
        import json
        import tempfile
        from mizu.process import Result
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        web = Web({"hosts": [], "feeds": [], "cache_seconds": 0, "timeout_seconds": 5,
                   "max_bytes": 1048576, "search_command": ["python3", "adapter"],
                   "intranet": False}, tmp / "c", tmp / "r")
        import mizu.web as webmod
        # 35 small results are retained in full: no hidden fixed barrier.
        results = [{"title": "t", "url": "https://x/", "summary": "s"} for _ in range(35)]
        with patch.object(webmod, "run", return_value=Result(0, json.dumps({"results": results}), "", "exited", 0.1)):
            out = web.search("q")
        self.assertEqual(len(out["results"]), 35)
        self.assertFalse(out["truncated"])
        # Beyond the documented limit the oldest-shaped overflow is cut and flagged.
        many = [{"title": "t", "url": "https://x/", "summary": "s"} for _ in range(SEARCH_RESULT_LIMIT + 5)]
        with patch.object(webmod, "run", return_value=Result(0, json.dumps({"results": many}), "", "exited", 0.1)):
            out = web.search("q")
        self.assertEqual(len(out["results"]), SEARCH_RESULT_LIMIT)
        self.assertTrue(out["truncated"])

    def test_adapter_also_consults_feeds_with_source_labels(self):
        from mizu.web import Web
        import json
        import tempfile
        from mizu.process import Result
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        feed = "https://example.org/feed"
        web = Web({"hosts": ["example.org"], "feeds": [feed], "cache_seconds": 0,
                   "timeout_seconds": 5, "max_bytes": 1048576,
                   "search_command": ["python3", "adapter"], "intranet": False},
                  tmp / "c", tmp / "r")
        web.fetch = lambda url: {"id": "r", "text": "<rss><channel><item><title>flaky builds</title>"
                    "<link>https://example.org/paper</link><description>d</description></item></channel></rss>"}
        import mizu.web as webmod
        adapted = [{"title": "t", "url": "https://x/", "summary": "s"}]
        with patch.object(webmod, "run", return_value=Result(0, json.dumps({"results": adapted}), "", "exited", 0.1)):
            out = web.search("flaky")
        self.assertEqual(out["scope"], "configured-search-adapter+feeds")
        self.assertEqual(out["feeds_total"], 1)
        self.assertEqual(out["feeds_consulted"], 1)
        self.assertEqual(out["errors"], [])
        self.assertEqual(len(out["results"]), 2)
        # Adapter keeps ranking priority; every result names its source.
        self.assertEqual(out["results"][0]["source"], "search-adapter")
        self.assertEqual(out["results"][1]["source"], feed)
        self.assertFalse(out["truncated"])

    def test_adapter_priority_fills_result_bound_first(self):
        from mizu.web import Web, SEARCH_RESULT_LIMIT
        import json
        import tempfile
        from mizu.process import Result
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        web = Web({"hosts": ["example.org"], "feeds": ["https://example.org/feed"],
                   "cache_seconds": 0, "timeout_seconds": 5, "max_bytes": 1048576,
                   "search_command": ["python3", "adapter"], "intranet": False},
                  tmp / "c", tmp / "r")
        web.fetch = lambda url: {"id": "r", "text": "<rss><channel><item><title>q</title>"
                    "<link>https://example.org/y</link><description>d</description></item></channel></rss>"}
        import mizu.web as webmod
        adapted = [{"title": "t", "url": "https://x/", "summary": "s"} for _ in range(SEARCH_RESULT_LIMIT)]
        with patch.object(webmod, "run", return_value=Result(0, json.dumps({"results": adapted}), "", "exited", 0.1)):
            out = web.search("q")
        self.assertEqual(len(out["results"]), SEARCH_RESULT_LIMIT)
        self.assertTrue(all(item["source"] == "search-adapter" for item in out["results"]))
        self.assertTrue(out["truncated"])
        self.assertEqual(out["feeds_consulted"], 1)


class DriverSeparationTests(Fixture):
    def test_pi_does_not_rewrite_operator_settings(self):
        from mizu.pi import PiDriver
        tmp=self.config.agent_dir('pi')
        mkdir(tmp)
        write_json(tmp/'settings.json',{'operator':'retained'})
        ctx=self.context('consult')
        import mizu.pi as module
        with patch.object(module,'Bridge',side_effect=OSError('before process start')):
            with self.assertRaises(OSError): PiDriver(self.config).execute(ctx,'test')
        self.assertEqual(read_json(tmp/'settings.json'),{'operator':'retained'})
        self.assertEqual(read_json(ctx.run_dir/'pi-effective.json')['model'],'test-model')


class PresentationSeparationTests(Fixture):
    def test_report_is_verbatim_with_evidence_separate(self):
        from mizu.report import publish
        snap = self.project.snapshots.get()
        result = publish(self.project, snap, {"title": "T", "body": "plain body"})
        text = Path(result["markdown"]).read_text()
        self.assertTrue(text.startswith("# T\n\nplain body"))
        self.assertNotIn("Snapshot:", text)
        self.assertNotIn("As of:", text)

    def test_prompt_respects_pending_knob(self):
        import dataclasses
        for n in range(5):
            self.project.insights.submit(source="s", title=f"D{n}", body="b", base_snapshot=None)
        small = dataclasses.replace(self.config, limits=dataclasses.replace(
            self.config.limits, pending_insights=2, retention_days=60))
        self.project.config = small
        projection = self.project.insights.projection(limit=small.limits.pending_insights)
        self.assertLessEqual(len(projection["items"]), 2)

    def test_cli_single_role_fallback(self):
        import dataclasses
        from mizu.cli import _resolve_role
        from mizu.errors import ConfigError
        single = {"only": self.config.roles["consult"]}
        solo = dataclasses.replace(self.config, roles=single, consult_role="")
        self.assertEqual(_resolve_role(solo, None), "only")
        self.assertEqual(_resolve_role(solo, None, probe=True), "only")
        with self.assertRaises(ConfigError):
            multi = dataclasses.replace(self.config, roles={})
            _resolve_role(multi, None)

    def test_init_roles_default_resolves_without_hardcoded_worker(self):
        from mizu.cli import _resolve_role, parser
        from mizu.errors import ConfigError
        args = parser().parse_args(["init", "demo", "--source", "s", "--goal", "g"])
        self.assertIsNone(args.roles)
        with self.assertRaises(ConfigError):
            _resolve_role(self.config, None)
        import dataclasses
        single = {"only": self.config.roles["consult"]}
        solo = dataclasses.replace(self.config, roles=single)
        self.assertEqual(_resolve_role(solo, None), "only")

    def test_service_floors_are_named_constants(self):
        import mizu.services as svc
        for name in ("RESTART_SEC", "ACCURACY_SEC", "RANDOMIZED_DELAY_SEC"):
            self.assertTrue(hasattr(svc, name))
        units = svc.render(self.config, self.project, ROOT / "bin/mizu", system="linux")
        text = "".join(units.values())
        self.assertIn("RestartSec=15", text)
        self.assertIn("AccuracySec=10s", text)

if __name__ == "__main__":
    raise SystemExit(unittest.main())
