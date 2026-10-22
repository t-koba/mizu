import hashlib
import json
from pathlib import Path
from support import Fixture
from mizu.cli import execute, parser
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
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), before)

    def test_cli_usage(self):
        self.write_run("i" * 32, finished_at="2026-09-05T00:00:00+00:00",
                       usage=[{"input_tokens": 11, "output_tokens": 2}])
        args = parser().parse_args(["--config", str(self.file), "usage", "sample"])
        result = execute(args)
        self.assertEqual(result["totals"]["input_tokens"], 11)
        self.assertIn("Not a bill", result["note"])

    def test_usage_entries_keep_timestamps(self):
        self.write_run("j" * 32, finished_at="2026-09-06T22:30:00+00:00",
                       usage=[{"input_tokens": 4, "output_tokens": 1}])
        facts = summarize(self.project)
        self.assertEqual(facts["totals"]["input_tokens"], 4)
        self.assertEqual(facts["recent_entries"][0]["finished_at"],
                         "2026-09-06T22:30:00+00:00")

    def test_day_buckets_follow_configured_timezone(self):
        # 15:00 UTC is the next local day in +09:00 zones, so UTC and
        # zone-configured buckets must disagree by one day here.
        import dataclasses
        try:
            config = dataclasses.replace(self.config, timezone="Asia/Tokyo")
            from mizu.config import resolve_timezone
            resolve_timezone("Asia/Tokyo")
        except Exception as exc:
            self.skipTest(f"tz database unavailable: {exc}")
        from mizu.project import Project
        project = Project(config, "sample")
        run_dir = project.root / "runs" / ("t" * 32)
        from mizu.fs import mkdir, write_json
        mkdir(run_dir)
        write_json(run_dir / "started.json", {"run": "t" * 32, "role": "worker",
                                              "started_at": "2026-09-01T15:00:00+00:00"})
        write_json(run_dir / "result.json", {"run": "t" * 32, "role": "worker", "status": "completed",
                                             "finished_at": "2026-09-01T15:00:00+00:00", "finish": {},
                                             "model": {"profile": "primary", "provider": "acme",
                                                       "model": "m1", "requests": 1,
                                                       "usage": [{"input_tokens": 1}]},
                                             "snapshot": "x"})
        self.assertEqual(summarize(self.project)["groups"][0]["day"], "2026-09-01")
        self.assertEqual(summarize(project)["groups"][0]["day"], "2026-09-02")
class CacheAffinityTests(Fixture):
    KEY = "ab" * 32

    def write_usage_run(self, name, **kwargs):
        run_dir = self.project.root / "runs" / name
        mkdir(run_dir)
        finished = kwargs.pop("finished_at", "2026-09-10T00:00:00+00:00")
        write_json(run_dir / "started.json", {"run": name, "role": "worker", "started_at": finished})
        write_json(run_dir / "result.json", {"run": name, "role": "worker", "status": "completed",
                                             "finished_at": finished, "finish": {},
                                             "model": {"profile": "primary", "provider": "acme",
                                                       "model": "m1", "requests": 1, "usage": []},
                                             "snapshot": "x"})
        return run_dir

    def attempt(self, index, *, key=KEY, generation=3, mode="full",
                status="partial", input_tokens=100, cache_read=25,
                role="worker", engine="pi", extra_session=None, extra=None):
        session = {"key": key, "generation": generation, "prompt_mode": mode,
                   "rotation": None, "unknown_reason": None}
        if extra_session:
            session.update(extra_session)
        record = {"execution_attempt": index, "run": "r", "role": role,
                  "engine": engine, "started_at": "2026-09-10T00:00:00+00:00",
                  "ended_at": "2026-09-10T00:01:00+00:00", "status": "returned",
                  "session": session,
                  "usage": {"input_tokens": input_tokens, "cache_read_tokens": cache_read,
                            "status": status}}
        if extra:
            record.update(extra)
        return record

    def test_share_math(self):
        from mizu.usage import cache_read_share
        self.assertEqual(cache_read_share(100, 25), 0.25)
        self.assertEqual(cache_read_share(8, 8), 1.0)
        self.assertIsNone(cache_read_share(0, 0))
        self.assertIsNone(cache_read_share(10, 20))
        self.assertIsNone(cache_read_share(None, 5))
        self.assertIsNone(cache_read_share(10.0, 2))
        self.assertIsNone(cache_read_share(True, 1))
        run_dir = self.write_usage_run("a" * 32)
        write_json(run_dir / "dispatch-attempts.json",
                   {"schema": 1, "dropped_attempts": 0, "attempts": [
                       self.attempt(0, input_tokens=100, cache_read=25),
                       self.attempt(1, input_tokens=0, cache_read=0),
                       self.attempt(2, input_tokens=10, cache_read=20)]})
        facts = summarize(self.project)
        rows = {r["execution_attempt"]: r for r in facts["cache_affinity"]["attempts"]}
        self.assertEqual(rows[0]["cache_read_share_of_input"], 0.25)
        self.assertIsNone(rows[1]["cache_read_share_of_input"])
        self.assertIsNone(rows[2]["cache_read_share_of_input"])

    def test_invalid_and_unknown_identity(self):
        run_dir = self.write_usage_run("b" * 32)
        valid = self.attempt(0)
        bad_key = self.attempt(1, key="AB" * 32)
        bad_gen = self.attempt(2, generation=-1)
        unknown = self.attempt(3, key=None, generation=None, mode=None)
        unknown["session"] = {"key": None, "generation": None, "prompt_mode": None,
                              "rotation": None, "unknown_reason": "driver_session_unobserved"}
        leaky = self.attempt(4, extra_session={"session_id": "native-123", "provider_session": "s-1"})
        write_json(run_dir / "dispatch-attempts.json",
                   {"schema": 1, "dropped_attempts": 0,
                    "attempts": [valid, bad_key, bad_gen, unknown, leaky]})
        facts = summarize(self.project)
        rows = {r["execution_attempt"]: r for r in facts["cache_affinity"]["attempts"]}
        self.assertEqual((rows[0]["session_key"], rows[0]["session_generation"], rows[0]["prompt_mode"]),
                         (self.KEY, 3, "full"))
        for index in (1, 2, 3):
            self.assertIsNone(rows[index]["session_key"])
            self.assertIsNone(rows[index]["session_generation"])
            self.assertIsNone(rows[index]["prompt_mode"])
        self.assertEqual(rows[4]["session_key"], self.KEY)
        for row in rows.values():
            self.assertNotIn("session_id", row)
            self.assertNotIn("provider_session", row)
            self.assertNotIn("unknown_reason", row)
            self.assertNotIn("rotation", row)
        self.assertEqual(set(rows[0]),
                         {"run", "role", "execution_attempt", "started_at", "ended_at", "engine",
                          "session_key", "session_generation", "prompt_mode", "usage_status",
                          "input_tokens", "cache_read_tokens", "cache_read_share_of_input"})

    def test_bounded_truncated_most_recent_first(self):
        import os
        base = 1000.0
        for n in range(3):
            name = f"run-{n:032d}"
            run_dir = self.write_usage_run(name)
            attempts = [self.attempt(i, input_tokens=10, cache_read=1) for i in range(100)]
            write_json(run_dir / "dispatch-attempts.json",
                       {"schema": 1, "dropped_attempts": 0, "attempts": attempts})
            stamp = base + n
            os.utime(run_dir, (stamp, stamp))
            os.utime(run_dir / "dispatch-attempts.json", (stamp, stamp))
        facts = summarize(self.project)
        affinity = facts["cache_affinity"]
        self.assertEqual(affinity["total"], 300)
        self.assertTrue(affinity["truncated"])
        self.assertEqual(len(affinity["attempts"]), 200)
        self.assertEqual(affinity["attempts"][0]["run"], "run-00000000000000000000000000000002")
        self.assertEqual(affinity["attempts"][0]["execution_attempt"], 99)
        self.assertEqual(affinity["runs_scanned"], 3)
        self.assertFalse(affinity["runs_truncated"])

    def test_run_window_is_bounded_and_marked(self):
        import os
        for n in range(205):
            run_dir = self.project.root / "runs" / f"window-{n:032d}"
            mkdir(run_dir)
            write_json(run_dir / "dispatch-attempts.json",
                       {"schema": 1, "dropped_attempts": 0, "attempts": [self.attempt(0)]})
            stamp = 2000.0 + n
            os.utime(run_dir, (stamp, stamp))
        affinity = summarize(self.project)["cache_affinity"]
        self.assertEqual(affinity["runs_scanned"], 200)
        self.assertTrue(affinity["runs_truncated"])
        self.assertEqual(affinity["total"], 200)
        self.assertFalse(affinity["truncated"])
        self.assertEqual(len(affinity["attempts"]), 200)

    def test_symlink_and_malformed_skip(self):
        good = self.write_usage_run("c" * 32)
        write_json(good / "dispatch-attempts.json",
                   {"schema": 1, "dropped_attempts": 0, "attempts": [self.attempt(0)]})
        broken = self.write_usage_run("d" * 32)
        (broken / "dispatch-attempts.json").write_text("{broken")
        linked = self.write_usage_run("e" * 32)
        try:
            (linked / "dispatch-attempts.json").symlink_to(good / "dispatch-attempts.json")
        except OSError:
            self.skipTest("symlinks unavailable")
        shapeless = self.write_usage_run("g" * 32)
        write_json(shapeless / "dispatch-attempts.json", {"schema": 1})
        facts = summarize(self.project)
        affinity = facts["cache_affinity"]
        self.assertGreaterEqual(affinity["skipped_records"], 3)
        self.assertEqual(facts["totals"]["runs"], 4)
        self.assertTrue(all(r["run"] == "c" * 32 for r in affinity["attempts"]))

    def test_cli_presence_preserves_existing_output(self):
        self.write_usage_run("h" * 32, usage=None)
        args = parser().parse_args(["--config", str(self.file), "usage", "sample"])
        result = execute(args)
        self.assertIn("cache_affinity", result)
        self.assertIn("attempts", result["cache_affinity"])
        self.assertIn("total", result["cache_affinity"])
        self.assertIn("truncated", result["cache_affinity"])
        self.assertIn("skipped_records", result["cache_affinity"])
        self.assertIn("runs_scanned", result["cache_affinity"])
        self.assertIn("runs_truncated", result["cache_affinity"])
        self.assertIn("recent_entries", result)
        self.assertIn("Not a bill", result["note"])
