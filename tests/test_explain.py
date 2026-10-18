"""Read-only per-role evidence projection; no scheduler or filesystem side effects."""
import json
import unittest
from unittest.mock import patch

from support import Fixture
from mizu.budget import Budget
from mizu.cli import execute, parser
from mizu.errors import ConfigError
from mizu.insights import Insights
from mizu.project import Project
from mizu.runtime import Engine


class ExplainTests(Fixture):
    def _args(self, *extra):
        return parser().parse_args(["--config", str(self.file), "explain", "sample", *extra])

    def _tree_state(self, root):
        return {path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in root.rglob("*") if path.is_file() and not path.is_symlink()}

    def test_cli_projects_one_role_and_avoids_mutating_or_unbounded_apis(self):
        health = self.project.root / "health" / "worker.json"
        health.parent.mkdir(exist_ok=True)
        health.write_text(json.dumps({"consecutive_failures": 2, "last_run": "abcdef0123456789abcdef0123456789",
                                      "updated_at": "2026-01-02T03:04:05+00:00", "error": "private error text"}))
        active = self.project.root / "active" / "worker.json"
        active.parent.mkdir(exist_ok=True)
        active.write_text(json.dumps({"run": "0123456789abcdef0123456789abcdef", "role": "worker",
                                      "started_at": "2026-01-02T03:04:05+00:00"}))
        Budget(self.config.data / "budget", self.config.limits.daily_requests,
               self.config.limits.retention_days,
               self.config.limits.shared_daily_requests, self.config.timezone).take(
                   "recorded-request", project="sample")
        before = self._tree_state(self.config.data)
        with (patch.object(Project, "status", side_effect=AssertionError("status called")),
              patch.object(Budget, "gc", side_effect=AssertionError("gc called")),
              patch.object(Insights, "list", side_effect=AssertionError("inbox scanned")),
              patch.object(Insights, "due_waits", side_effect=AssertionError("waits scanned")),
              patch.object(Insights, "pending_obligations", side_effect=AssertionError("waits scanned")),
              patch.object(Engine, "preview", side_effect=AssertionError("selection preview called"))):
            result = execute(self._args("--role", "worker"))
        self.assertEqual(before, self._tree_state(self.config.data))
        self.assertEqual(result["project"], "sample")
        self.assertEqual(result["role"], "worker")
        self.assertEqual(result["schema_version"], 1)
        self.assertTrue(result["generated_at"])
        self.assertIn("Non-atomic", result["observation"])
        self.assertEqual(result["project_facts"]["snapshot"]["scope"], "project")
        self.assertEqual(result["project_facts"]["snapshot"]["record"]["outcome"],
                         self.project.snapshots.get()["outcome"])
        self.assertEqual(result["project_facts"]["budget"]["counters"]["used"], 1)
        facts = result["role_facts"]
        self.assertEqual(facts["health"]["record"]["consecutive_failures"], 2)
        self.assertNotIn("error", facts["health"]["record"])
        self.assertEqual(facts["active"]["process_liveness"], "unknown")
        self.assertEqual(facts["active"]["yieldability"], "unknown")
        self.assertNotIn("last_completed_outcome", facts["unknowns"])
        self.assertEqual(facts["last_outcome"], {"status": "unavailable", "record": None})
        self.assertEqual(facts["selection_wait"], {"status": "absent", "record": None})
        self.assertEqual(facts["unknowns"]["pending_insight_state"], "unknown")
        self.assertEqual(facts["unknowns"]["structured_wait_state"], "unknown")
        self.assertIn("decision_sources", facts["triggers"])
        self.assertEqual(facts["selection_constraints"]["status"], "not_configured")
        self.assertEqual(facts["selection_constraints"]["configured_profile"]["profile"], "primary")
        self.assertEqual(result["project_facts"]["control"]["record"]["reason"],
                         "Initialized unarmed; operator arm required")
        serialized = json.dumps(result)
        self.assertNotIn("private error text", serialized)
        self.assertNotIn("will_run", result)

    def test_role_is_required_for_multi_role_project_and_must_be_enabled(self):
        with self.assertRaises(ConfigError):
            execute(self._args())
        with self.assertRaises(ConfigError):
            execute(self._args("--role", "missing"))

    def test_single_enabled_role_is_the_default(self):
        path = self.project.root / "project.toml"
        path.write_text('roles = ["worker"]\nverify = []\n')
        result = execute(self._args())
        self.assertEqual(result["role"], "worker")

    def test_selector_probe_explanation_keeps_sponsor_rule_and_due_bound(self):
        import time
        from types import SimpleNamespace
        from mizu.explain import _selection
        from mizu.fs import write_json
        from mizu.selection import State

        now = time.time()
        identity = "b" * 64
        contribution = {
            "key": "group:opencode-go", "profile": "primary", "sponsor": "reviewer",
            "role": "reviewer", "sponsor_binding": "a" * 64, "target": "c" * 64,
            "selector": "dynamic", "rule": 0, "error_rule": 1,
            "created_at": now - 60, "until": now + 3600, "probe_seconds": 30,
            "next_probe_at": now - 1, "status": "active", "revision": 1,
            "claim": None, "last_event": None, "reason": "launcher",
        }
        state = {"version": 2, "observations": {}, "blocks": {}, "history": {},
                 "contributions": {identity: contribution},
                 "receipts": {identity: {"payload": "d" * 64, "action": {
                     "rule": 1, "until": contribution["until"], "scope": "group",
                     "probe_seconds": 30, "contribution": identity}}},
                 "events": {}, "exhausted": False}
        write_json(State(self.config).path, state)
        config = SimpleNamespace(data=self.config.data, selectors={"dynamic": {"rules": [
            {"candidates": [{"profile": "primary", "group": "opencode-go"}]}]}})
        role = SimpleNamespace(selector="dynamic", profile="primary")

        result = _selection(config, role)
        probe = result["probe_blocks"][0]
        self.assertEqual(probe["sponsor"], "reviewer")
        self.assertEqual(probe["sponsor_role"], "reviewer")
        self.assertEqual(probe["rule"], 0)
        self.assertEqual(probe["error_rule"], 1)
        self.assertEqual(probe["until"], contribution["until"])
        self.assertTrue(probe["probe_due"])
        self.assertEqual(probe["sponsor_authorization"], "not_checked")
        self.assertEqual(result["current_choice"], "unknown")

    def test_missing_snapshot_directories_are_not_created_by_read_only_command(self):
        for directory in ("objects", "snapshots"):
            path = self.project.root / directory
            import shutil
            shutil.rmtree(path)
        result = execute(self._args("--role", "worker"))
        self.assertEqual(result["project_facts"]["snapshot"]["status"], "unavailable")
        self.assertFalse((self.project.root / "objects").exists())
        self.assertFalse((self.project.root / "snapshots").exists())

    def test_deeply_nested_snapshot_is_reported_unavailable(self):
        from mizu.explain import _snapshot

        snapshot = self.project.snapshots.get()
        path = self.project.root / "snapshots" / (snapshot["id"] + ".json")
        path.write_text("[" * 10000 + "]" * 10000)
        self.assertEqual(_snapshot(self.project), {
            "status": "unavailable", "scope": "project", "record": None})

    def test_last_outcome_reports_bounded_run_record(self):
        run = "0123456789abcdef0123456789abcdef"
        health = self.project.root / "health" / "worker.json"
        health.parent.mkdir(exist_ok=True)
        health.write_text(json.dumps({"consecutive_failures": 0, "last_run": run,
                                      "updated_at": "2026-01-02T03:04:05+00:00"}))
        runs = self.project.root / "runs" / run
        runs.mkdir(parents=True)
        (runs / "result.json").write_text(json.dumps(
            {"run": run, "role": "worker", "status": "prepared",
             "finished_at": "2026-01-03T03:04:05+00:00", "extra": [1, 2, 3]}))
        result = execute(self._args("--role", "worker"))
        self.assertEqual(result["role_facts"]["last_outcome"], {
            "status": "current",
            "record": {"kind": "result", "status": "prepared",
                       "finished_at": "2026-01-03T03:04:05+00:00"}})

    def test_last_outcome_prefers_error_record_and_fails_closed(self):
        run = "abcdef0123456789abcdef0123456789"
        health = self.project.root / "health" / "worker.json"
        health.parent.mkdir(exist_ok=True)
        health.write_text(json.dumps({"consecutive_failures": 1, "last_run": run,
                                      "updated_at": "2026-01-02T03:04:05+00:00"}))
        runs = self.project.root / "runs" / run
        runs.mkdir(parents=True)
        (runs / "error.json").write_text(json.dumps(
            {"run": run, "role": "worker", "status": "interrupted",
             "finished_at": "2026-01-04T03:04:05+00:00"}))
        result = execute(self._args("--role", "worker"))
        self.assertEqual(result["role_facts"]["last_outcome"]["record"]["kind"], "error")
        self.assertEqual(result["role_facts"]["last_outcome"]["record"]["status"], "interrupted")
        # A malformed run record fails closed to unavailable, never a guess.
        (runs / "error.json").write_text(json.dumps({"status": 42}))
        result = execute(self._args("--role", "worker"))
        self.assertEqual(result["role_facts"]["last_outcome"],
                         {"status": "unavailable", "record": None})

    def test_selection_wait_reports_probe_wait_without_scans(self):
        from mizu import explain as explain_module
        from unittest.mock import patch
        selection = self.project.root / "selection"
        selection.mkdir(exist_ok=True)
        (selection / "worker-wait.json").write_text(json.dumps(
            {"fingerprint": "ab" * 32, "run": "0123456789abcdef0123456789abcdef",
             "next_evaluation_at": 1790000000.0, "version": 1}))
        with (patch.object(explain_module, "_read_record",
                           wraps=explain_module._read_record) as spy):
            result = execute(self._args("--role", "worker"))
        self.assertEqual(result["role_facts"]["selection_wait"], {
            "status": "current",
            "record": {"next_evaluation_at": 1790000000.0,
                       "run": "0123456789abcdef0123456789abcdef"}})
        # Only single bounded record reads; no inbox or wait scans.
        for call in spy.call_args_list:
            self.assertTrue(call.args[1].endswith(".json"))
            self.assertNotIn("inbox", call.args[1])
            self.assertNotIn("waits", call.args[1].split("/"))

    def test_unreadable_role_record_fails_closed_and_emits_no_file_text(self):
        health = self.project.root / "health" / "worker.json"
        health.parent.mkdir(exist_ok=True)
        health.write_text("{" + " " * 70000 + "secret marker")
        result = execute(self._args("--role", "worker"))
        self.assertEqual(result["role_facts"]["health"], {"status": "unavailable", "record": None})
        self.assertNotIn("secret marker", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
