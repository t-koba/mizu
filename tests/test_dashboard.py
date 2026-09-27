import html
import json
import subprocess
import sys
from pathlib import Path
from support import Fixture, ScriptDriver, ROOT
from mizu.cli import execute, parser
from mizu.dashboard import collect, publish
from mizu.runtime import Engine


class DashboardTests(Fixture):
    def test_publish_creates_entry_and_pointer_without_html(self):
        result = publish(self.project)
        entry = Path(result["document"])
        self.assertTrue(entry.is_file())
        self.assertEqual(entry.stem, result["dashboard"])
        self.assertFalse(list(self.project.root.rglob("*.html")))
        payload = json.loads(entry.read_text())
        self.assertEqual(payload["schema"], 1)
        self.assertEqual(payload["project"], "sample")
        self.assertIn("pending_insights", payload)
        latest = json.loads((self.project.root / "dashboard/latest.json").read_text())
        self.assertEqual(latest["dashboard"], result["dashboard"])
        self.assertEqual(latest["snapshot"], result["snapshot"])

    def test_pending_and_answered_counts(self):
        snap = self.project.snapshots.get()["id"]
        item = self.project.insights.submit(source="operator", title="Pick one", body="A or B?",
                                            base_snapshot=snap)
        core = collect(self.project)
        self.assertEqual(core["pending_count"], 1)
        self.assertEqual(core["answered_count"], 0)
        self.assertEqual(core["pending_insights"][0]["id"], item["id"])
        self.assertIsNone(core["pending_insights"][0]["decision"])
        self.project.insights.decide(item["id"], "accept", "A fits the goal", "", "test")
        core = collect(self.project)
        self.assertEqual(core["pending_count"], 0)
        self.assertEqual(core["answered_count"], 1)
        self.assertEqual(core["recent_decisions"][0]["action"], "accept")

    def test_blocked_outcome_surfaces_needs_input(self):
        def blocked(ctx, *_):
            ctx.handle("finish", {"outcome": "blocked", "summary": "Need a call.",
                                  "state": "Q1: which backend? Tried both; input unblocks."})
        Engine(self.config, driver=ScriptDriver(blocked)).run(self.project, "worker")
        core = collect(self.project)
        self.assertTrue(core["needs_operator_input"])
        self.assertEqual(core["snapshot"]["outcome"], "blocked")
        self.assertIn("Q1", core["snapshot"]["state"])

    def test_payload_has_no_secrets_or_workspace_contents(self):
        (self.project.workspace / "secret-note.txt").write_text("SECRET-XYZ-123\n")
        payload = json.loads((Path(publish(self.project)["document"])).read_text())
        text = json.dumps(payload)
        self.assertNotIn("SECRET-XYZ-123", text)
        self.assertNotIn("credentials", text)

    def test_republish_is_idempotent(self):
        first = publish(self.project)
        second = publish(self.project)
        self.assertEqual(first["dashboard"], second["dashboard"])

    def test_cli_dashboard(self):
        args = parser().parse_args(["--config", str(self.file), "dashboard", "sample"])
        result = execute(args)
        self.assertIn("dashboard", result)
        self.assertTrue((self.project.root / "dashboard/latest.json").is_file())

    def test_renderer_escapes_and_adds_no_design(self):
        def blocked(ctx, *_):
            ctx.handle("finish", {"outcome": "blocked", "summary": "Need a call.",
                                  "state": "Q1: <script>alert(1)</script>"})
        Engine(self.config, driver=ScriptDriver(blocked)).run(self.project, "worker")
        publish(self.project)
        script = ROOT / "examples/render-dashboard.py"
        done = subprocess.run([sys.executable, str(script), str(self.project.root)],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        page = (self.project.root / "dashboard/index.html").read_text()
        self.assertIn("Content-Security-Policy", page)
        self.assertIn("viewport", page)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn(html.escape("Q1: <script>alert(1)</script>"), page)
        # Bare facts dump: raw keys visible, no designed sections or tables.
        self.assertIn("pending_insights", page)
        self.assertNotIn("<table", page)
        self.assertNotIn("http://", page.replace("http-equiv", ""))
