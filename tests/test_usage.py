import hashlib
import json
from pathlib import Path
from support import Fixture
from mizu.cli import execute, parser
from mizu.dashboard import publish
from mizu.fs import mkdir, write_json
from mizu.usage import normalize, summarize


class UsageTests(Fixture):
    def write_run(self, name, *, finished_at, provider="acme", model="m1",
                  requests=1, usage=None, status="completed", role="worker"):
        run_dir = self.project.root / "runs" / name
        mkdir(run_dir)
        write_json(run_dir / "started.json", {"run": name, "role": role, "started_at": finished_at})
        write_json(run_dir / "result.json", {"run": name, "role": role, "status": status,
                                             "finished_at": finished_at, "finish": {},
                                             "model": {"profile": "primary", "provider": provider,
                                                       "model": model, "requests": requests,
                                                       "usage": usage if usage is not None else []},
                                             "snapshot": "x"})

    def test_groups_sum_by_day_but_entries_keep_timestamps(self):
        # Same day, same model, different hours: one rate group, but the
        # per-run timestamps survive so time-of-day rates stay computable.
        self.write_run("a" * 32, finished_at="2026-09-01T02:00:00+00:00",
                       usage=[{"input_tokens": 100, "output_tokens": 10}])
        self.write_run("b" * 32, finished_at="2026-09-01T14:00:00+00:00",
                       usage=[{"input_tokens": 200, "output_tokens": 20}])
        facts = summarize(self.project)
        self.assertEqual(len(facts["groups"]), 1)
        group = facts["groups"][0]
        self.assertEqual((group["input_tokens"], group["output_tokens"]), (300, 30))
        self.assertEqual(facts["totals"]["input_tokens"], 300)
        hours = sorted(e["finished_at"][11:13] for e in facts["recent_entries"])
        self.assertEqual(hours, ["02", "14"])
        self.assertFalse(facts["truncated"])
        self.assertFalse(facts["entries_truncated"])

    def test_normalizes_shapes_and_surfaces_unknown(self):
        self.assertEqual(normalize({"inputTokens": 5, "outputTokens": 7}, "claude")["input_tokens"], 5)
        self.write_run("c" * 32, finished_at="2026-09-02T00:00:00+00:00",
                       usage=[{"input_tokens": 5, "output_tokens": 7},
                              {"input_tokens": 1, "cache_read_tokens": 50},
                              "garbage"])
        facts = summarize(self.project)
        group = facts["groups"][0]
        self.assertEqual(group["input_tokens"], 6)
        self.assertEqual(group["output_tokens"], 7)
        self.assertEqual(group["other_tokens"], 0)
        self.assertEqual(group["cache_read_tokens"], 50)
        self.assertEqual(group["unknown_shapes"], 1)
        self.assertEqual(facts["unknown_shapes"], 1)

    def test_skips_incomplete_and_malformed_without_raising(self):
        self.write_run("d" * 32, finished_at="2026-09-03T00:00:00+00:00", status="interrupted")
        self.write_run("e" * 32, finished_at="2026-09-03T00:00:00+00:00",
                       usage=[{"input_tokens": 9, "output_tokens": 1}])
        run_dir = self.project.root / "runs" / ("f" * 32)
        mkdir(run_dir)
        (run_dir / "result.json").write_text("{broken")
        facts = summarize(self.project)
        self.assertEqual(facts["totals"]["input_tokens"], 9)
        self.assertEqual(facts["scanned_records"], 2)

    def test_parent_consultation_cannot_duplicate_child_usage(self):
        run_dir = self.project.root / "runs" / ("g" * 32)
        mkdir(run_dir)
        model = {"provider": "acme", "model": "m2", "requests": 2,
                 "usage": [{"input_tokens": 3, "output_tokens": 4}]}
        write_json(run_dir / "consultation.json", {"question": "q", "answers": [{"run": "child", "model": model}]})
        child = self.project.root / "runs" / "child"
        mkdir(child)
        write_json(child / "consultation.json", {"run": "child", "parent_run": run_dir.name,
                   "role": "consult", "status": "completed", "finished_at": "2026-10-03T00:00:00+00:00", "model": model})
        facts = summarize(self.project)
        self.assertEqual(facts["totals"]["input_tokens"], 3)
        self.assertEqual(facts["totals"]["runs"], 1)
        self.assertEqual(facts["groups"][0]["model"], "m2")

    def test_raw_records_untouched(self):
        self.write_run("h" * 32, finished_at="2026-09-04T00:00:00+00:00",
                       usage=[{"input_tokens": 1, "output_tokens": 1}])
        target = self.project.root / "runs" / ("h" * 32) / "result.json"
        before = hashlib.sha256(target.read_bytes()).hexdigest()
        summarize(self.project)
        publish(self.project)
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), before)

    def test_cli_usage(self):
        self.write_run("i" * 32, finished_at="2026-09-05T00:00:00+00:00",
                       usage=[{"input_tokens": 11, "output_tokens": 2}])
        args = parser().parse_args(["--config", str(self.file), "usage", "sample"])
        result = execute(args)
        self.assertEqual(result["totals"]["input_tokens"], 11)
        self.assertIn("Not a bill", result["note"])

    def test_dashboard_embeds_usage_entries(self):
        self.write_run("j" * 32, finished_at="2026-09-06T22:30:00+00:00",
                       usage=[{"input_tokens": 4, "output_tokens": 1}])
        payload = json.loads(Path(publish(self.project)["document"]).read_text())
        self.assertEqual(payload["usage"]["totals"]["input_tokens"], 4)
        self.assertEqual(payload["usage"]["recent_entries"][0]["finished_at"],
                         "2026-09-06T22:30:00+00:00")
